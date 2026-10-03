"""
hypogeum/dashboard_clients.py — read-only aggregation for the Cortex
Advisory (client) dashboard. All reads. No writes. Safe from GET routes.

Public API:
    get_clients_overview(db_path)           -> dict
    get_client_overview(client_id, db_path) -> dict
    get_client_content(client_id, db_path)  -> dict
    get_client_accounts(client_id, db_path) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _content_rows(client_id: str, db_path) -> list[dict]:
    """content_items joined to their latest per-platform analytics."""
    from hypogeum.db import get_connection
    from hypogeum import client_manager as cm
    items = cm.list_content_items(client_id, db_path=db_path)
    if not items:
        return []
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT content_id, platform, MAX(snapshot_date) sd, "
            "  views, impressions, likes, comments, shares, saves, reach, engagement_rate, "
            "  avg_view_pct, retention_curve_json "
            "FROM platform_analytics WHERE client_id=? GROUP BY content_id, platform",
            (client_id,),
        ).fetchall()
    finally:
        conn.close()
    by_cid: dict = {}
    for r in rows:
        by_cid.setdefault(r["content_id"], {})[r["platform"]] = dict(r)
    out = []
    for it in items:
        metrics = by_cid.get(it["content_id"], {})
        total_views = sum((_num(m.get("views")) or 0) for m in metrics.values())
        # LinkedIn reports impressions, not views; old Instagram posts (before
        # a Business conversion) carry only likes/comments. Track both so a
        # post with real numbers is never shown as "no data".
        total_impr = sum((_num(m.get("impressions")) or 0) for m in metrics.values())
        interactions = sum((_num(m.get(k)) or 0) for m in metrics.values()
                           for k in ("likes", "comments", "shares", "saves"))
        ers = [_num(m.get("engagement_rate")) for m in metrics.values()
               if _num(m.get("engagement_rate")) is not None]
        rets = [_num(m.get("avg_view_pct")) for m in metrics.values()
                if _num(m.get("avg_view_pct")) is not None]
        out.append({
            **it,
            "platforms": sorted(metrics.keys()),
            "total_views": int(total_views),
            "total_impressions": int(total_impr),
            "interactions": int(interactions),
            "has_metrics": bool(total_views or total_impr or interactions or ers),
            "engagement_rate": round(sum(ers) / len(ers), 2) if ers else None,
            "avg_view_pct": round(sum(rets) / len(rets), 4) if rets else None,
            "has_retention": any(m.get("retention_curve_json") for m in metrics.values()),
        })
    return out


def get_clients_overview(db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    out: dict = {"clients": []}
    try:
        clients = cm.list_clients(db_path=db_path)
    except Exception:  # noqa: BLE001
        return out
    for c in clients:
        try:
            rows = _content_rows(c["client_id"], db_path)
        except Exception:  # noqa: BLE001
            rows = []
        try:
            accts = cm.list_managed_accounts(c["client_id"], db_path=db_path)
        except Exception:  # noqa: BLE001
            accts = []
        out["clients"].append({
            "client_id": c["client_id"], "name": c["name"], "status": c["status"],
            "engagement_mode": c["engagement_mode"],
            "stage": c.get("stage") or "active",
            "managed_since": c.get("managed_since"),
            "accounts": len(accts),
            "platforms": sorted({a["platform"] for a in accts}),
            "content_items": len(rows),
            "total_views": sum(r["total_views"] for r in rows),
            "with_analytics": len([r for r in rows if r["platforms"]]),
        })
    return out


def get_client_linkedin_snapshot(client_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Latest imported LinkedIn Export (hypogeum.publishing.linkedin_export):
    followers, period totals and the top audience values per category."""
    import json as _json
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        r = conn.execute("SELECT * FROM client_linkedin_snapshots WHERE client_id=? "
                         "ORDER BY period_end DESC LIMIT 1", (client_id,)).fetchone()
    except Exception:  # noqa: BLE001 — table absent on an old DB
        r = None
    finally:
        conn.close()
    if not r:
        return None
    demo = _json.loads(r["audience_demographics_json"] or "[]")
    by_cat: dict = {}
    for d in demo:
        by_cat.setdefault(d["category"], []).append(d)
    daily = _json.loads(r["daily_json"] or "[]")
    return {
        "period_start": r["period_start"], "period_end": r["period_end"],
        "followers_total": r["followers_total"], "impressions": r["impressions"],
        "members_reached": r["members_reached"],
        "new_followers": sum((d.get("new_followers") or 0) for d in daily),
        "audience": {c: v[:5] for c, v in by_cat.items()},
        "imported_at": r["imported_at"],
    }


def get_client_overview(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    out: dict = {"client": None, "content": [], "top": [], "bottom": [],
                 "kpis": {}, "audience": {}, "observed_audience": None,
                 "best_time": [], "monetization": None, "advisory": None,
                 "comments": []}
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return out
    out["client"] = client
    out["accounts"] = cm.list_managed_accounts(client_id, db_path=db_path)

    rows = _content_rows(client_id, db_path)
    out["content"] = rows
    scored = [r for r in rows if r["engagement_rate"] is not None]
    scored.sort(key=lambda r: r["engagement_rate"], reverse=True)
    out["top"] = scored[:3]
    out["bottom"] = scored[-3:][::-1] if len(scored) > 3 else []
    out["kpis"] = {
        "content_items": len(rows),
        "total_views": sum(r["total_views"] for r in rows),
        "total_impressions": sum(r["total_impressions"] for r in rows),
        "interactions": sum(r["interactions"] for r in rows),
        "with_metrics": sum(1 for r in rows if r["has_metrics"]),
        "avg_engagement": (round(sum(r["engagement_rate"] for r in scored) / len(scored), 2)
                           if scored else None),
        "engagement_basis": len(scored),
        "tracked": len([r for r in rows if r["platforms"]]),
    }

    out["linkedin"] = get_client_linkedin_snapshot(client_id, db_path=db_path)
    try:
        from hypogeum.publishing.zernio_native_sync import get_channel_audience
        out["audience"] = get_channel_audience(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.audience_profile import summarize_observed_audience
        out["observed_audience"] = summarize_observed_audience(client_id, db_path=db_path,
                                                               for_display=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.publishing.zernio_aggregate_sync import get_rollup
        bt = get_rollup("best_time", scope=f"client:{client_id}", db_path=db_path)
        from hypogeum.publishing.zernio_aggregate_sync import reliable_best_time_slots
        slots = reliable_best_time_slots((bt or {}).get("payload"))[:5] if bt else []
        out["best_time"] = [
            {"day": _DOW[int(s["day_of_week"])] if 0 <= int(s["day_of_week"]) < 7 else "?",
             "hour": int(s["hour"]), "engagement": round(s.get("avg_engagement") or 0, 1),
             "posts": s.get("post_count")}
            for s in slots]
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.monetization_eligibility import compute_client_monetization
        out["monetization"] = compute_client_monetization(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.publishing.zernio_native_sync import get_recent_audience_comments
        out["comments"] = get_recent_audience_comments(client_id, limit=8, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    return out


def get_client_content(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {"client": None, "content": []}
    return {"client": client, "content": _content_rows(client_id, db_path)}


def get_client_accounts(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {"client": None, "accounts": [], "zernio_pool": []}
    accts = cm.list_managed_accounts(client_id, db_path=db_path)
    linked_ids = {a["provider_account_id"] for a in accts if a.get("provider_account_id")}
    pool = []
    try:
        import json as _json
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            rows = conn.execute(
                "SELECT provider_account_id, platform, display_label, status, "
                "needs_reconnection, metadata_json FROM provider_accounts "
                "WHERE provider_id='zernio' ORDER BY platform"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                meta = _json.loads(r["metadata_json"] or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            pool.append({
                "platform": r["platform"],
                "provider_account_id": r["provider_account_id"],
                "username": meta.get("username") or r["display_label"],
                "followers": meta.get("followers_count"),
                "profile_id": meta.get("zernio_profile_id"),
                "status": r["status"],
                "needs_reconnection": bool(r["needs_reconnection"]),
                "linked_here": r["provider_account_id"] in linked_ids,
            })
    except Exception:  # noqa: BLE001
        pass
    return {"client": client, "accounts": accts, "zernio_pool": pool}


# ---------------------------------------------------------------------------
# Personal-branding plan (docs/plans/PERSONAL_BRANDING_PLAN.md) — stored in
# clients.brief: brand_profile / brand_strategy / content_calendar.
# ---------------------------------------------------------------------------

_POST_STATUSES = ("draft", "approved", "published", "skipped")


def calendars_of(brief: dict) -> dict:
    """{'YYYY-MM': calendar} — the per-month store (brief.content_calendars),
    with the legacy single brief.content_calendar folded in."""
    cals = dict((brief or {}).get("content_calendars") or {})
    legacy = (brief or {}).get("content_calendar")
    if legacy and legacy.get("month") and legacy["month"] not in cals:
        cals[legacy["month"]] = legacy
    return cals


def default_month(cals: dict, today=None) -> Optional[str]:
    """The current month if it has a calendar, else the nearest upcoming one,
    else the latest."""
    from datetime import datetime, timezone
    cur = (today or datetime.now(timezone.utc).date()).strftime("%Y-%m")
    if cur in cals:
        return cur
    future = sorted(m for m in cals if m > cur)
    return future[0] if future else (max(cals) if cals else None)


def _pending_ahead(cals: dict) -> int:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for cal in cals.values() for p in (cal.get("posts") or [])
               if (p.get("status") or "draft") in ("draft", "approved") and p.get("date", "") > today)


def get_client_plan(client_id: str, db_path: Optional[Path] = None, month: Optional[str] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {"client": None}
    brief = client.get("brief") or {}
    cals = calendars_of(brief)
    month = month if month in cals else default_month(cals)
    cal = cals.get(month) or {}
    posts = list(cal.get("posts") or [])
    counts = {s: sum(1 for p in posts if (p.get("status") or "draft") == s) for s in _POST_STATUSES}
    return {"client": client, "profile": brief.get("brand_profile") or {},
            "strategy": brief.get("brand_strategy") or {}, "calendar": cal, "posts": posts,
            "counts": counts, "months": sorted(cals), "month": month,
            "generation": brief.get("generation_status") or {},
            "drafts_ahead": int((brief.get("brand_profile") or {}).get("drafts_ahead") or 2),
            "pending_ahead": _pending_ahead(cals),
            "generation_log": list(brief.get("generation_log") or [])[-5:]}


def update_plan_post(client_id: str, date: str, *, status: Optional[str] = None,
                     draft: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """Edit one calendar post (matched by its date). Returns False if absent."""
    from datetime import datetime, timezone
    from hypogeum import client_manager as cm
    if status is not None and status not in _POST_STATUSES:
        raise ValueError(f"status must be one of {_POST_STATUSES}")
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return False
    brief = dict(client.get("brief") or {})
    cals = calendars_of(brief)
    cal = dict(cals.get((date or "")[:7]) or {})
    posts = list(cal.get("posts") or [])
    hit = False
    for p in posts:
        if p.get("date") == date:
            if status is not None:
                p["status"] = status
                if status == "published":
                    p["published_marked_at"] = datetime.now(timezone.utc).isoformat()
            if draft is not None and draft.strip():
                p["draft"] = draft.replace("\r\n", "\n").strip()
                p["edited_at"] = datetime.now(timezone.utc).isoformat()
            hit = True
    if not hit:
        return False
    cal["posts"] = posts
    cals[cal.get("month") or date[:7]] = cal
    brief["content_calendars"] = cals
    brief.pop("content_calendar", None)
    return cm.update_client(client_id, brief=brief, db_path=db_path)


# ---------------------------------------------------------------------------
# Brand questionnaire (dashboard) — fills clients.brief.brand_profile, the
# input the strategy / calendar are built from. One question per screen.
# ---------------------------------------------------------------------------

QUESTIONNAIRE = [
    {"key": "goal", "type": "single", "q": "What should the content achieve this year?",
     "options": ["More clients and projects", "Authority and reputation in the field",
                 "A bigger job or role", "Selling courses or products"], "other": True},
    {"key": "audience", "type": "multi", "q": "Who most needs to see it?",
     "options": ["Decision-makers at broadcasters and production companies", "Peers in the profession",
                 "Learners and beginners", "Creators and brands"], "other": True},
    {"key": "services", "type": "lines", "q": "What do you offer? One per line.",
     "hint": "e.g. Documentary editing, Color grading, AI ads"},
    {"key": "pillars", "type": "lines", "q": "What do you want to talk about? One topic per line.",
     "hint": "e.g. Before / after, Editing decisions, AI in production"},
    {"key": "tone", "type": "single", "q": "How should you come across?",
     "options": ["Calm, confident expert", "Storyteller", "Practical teacher", "Bold opinions"], "other": True},
    {"key": "posts_per_week", "type": "single", "q": "How many posts a week can you realistically approve?",
     "options": ["1", "2", "3", "5"]},
    {"key": "on_camera", "type": "single", "q": "Will you appear on camera?",
     "options": ["Yes", "No — the work speaks for itself"]},
    {"key": "language", "type": "single", "q": "Which language should posts be written in?",
     "options": ["English", "Arabic", "Both"]},
    {"key": "avoid", "type": "multi", "q": "Anything the content must stay away from?",
     "options": ["Politics", "Client names and numbers without permission", "Personal life", "Religion"],
     "other": True},
    {"key": "featured_projects", "type": "lines", "q": "Which projects should we talk about first? One per line.",
     "hint": "Name, client (if allowed), your role"},
    {"key": "awards", "type": "lines", "q": "Awards or recognitions worth mentioning? One per line.",
     "hint": "Award — year — project"},
    {"key": "assets_status", "type": "single", "q": "Where is the material (clips, before/after, stills)?",
     "options": ["On this computer", "On the archive", "On a drive / Vimeo / YouTube", "Scattered — still collecting"]},
]


def _as_lines(v) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [ln.strip() for ln in str(v or "").splitlines() if ln.strip()]


def questionnaire_prefill(profile: dict, strategy: dict) -> dict:
    """Current answers for the form, from brand_profile (and pillar names
    from an existing strategy)."""
    pre = dict(profile or {})
    if not pre.get("pillars") and (strategy or {}).get("pillars"):
        pre["pillars"] = [p.get("name") for p in strategy["pillars"] if p.get("name")]
    if isinstance(pre.get("awards"), list):
        pre["awards"] = [a if isinstance(a, str) else
                         " — ".join(str(x) for x in (a.get("name"), a.get("year"), a.get("project")) if x)
                         for a in pre["awards"]]
    if isinstance(pre.get("posts_per_week"), int):
        pre["posts_per_week"] = str(pre["posts_per_week"])
    if isinstance(pre.get("on_camera"), bool):
        pre["on_camera"] = "Yes" if pre["on_camera"] else "No — the work speaks for itself"
    langs = pre.get("languages")
    if langs and not pre.get("language"):
        pre["language"] = "Both" if len(langs) > 1 else ("Arabic" if langs == ["ar"] else "English")
    return pre


def save_questionnaire(client_id: str, form, db_path: Optional[Path] = None) -> bool:
    """Persist answers into clients.brief.brand_profile. `form` is a
    werkzeug MultiDict-like object (getlist/get). Keeps any other keys."""
    from datetime import datetime, timezone
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return False
    brief = dict(client.get("brief") or {})
    prof = dict(brief.get("brand_profile") or {})
    before = questionnaire_prefill(prof, brief.get("brand_strategy") or {})
    for q in QUESTIONNAIRE:
        k = q["key"]
        other = (form.get(f"{k}__other") or "").strip()
        if q["type"] == "multi":
            vals = [v for v in form.getlist(k) if v.strip()] + ([other] if other else [])
            if vals:
                prof[k] = vals
        elif q["type"] == "single":
            v = other or (form.get(k) or "").strip()
            if v:
                prof[k] = v
        else:
            lines = _as_lines(form.get(k) or "")
            # unchanged -> keep the stored value (e.g. structured award records)
            if lines and lines != _as_lines(before.get(k)):
                prof[k] = lines
    if prof.get("posts_per_week", "").isdigit() if isinstance(prof.get("posts_per_week"), str) else False:
        prof["posts_per_week"] = int(prof["posts_per_week"])
    if isinstance(prof.get("on_camera"), str):
        prof["on_camera"] = prof["on_camera"].lower().startswith("yes")
    lang = prof.get("language")
    if lang:
        prof["languages"] = {"English": ["en"], "Arabic": ["ar"], "Both": ["en", "ar"]}.get(lang, prof.get("languages"))
    prof["answered_at"] = datetime.now(timezone.utc).date().isoformat()
    brief["brand_profile"] = prof
    return cm.update_client(client_id, brief=brief, db_path=db_path)
