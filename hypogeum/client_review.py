"""
hypogeum/client_review.py — the monthly review of a client's content strategy (personal-branding plan, phase 6).

Once a month the factory measures what last month's posts did (Instagram from the Graph API, LinkedIn from the
monthly export -- hypogeum content_items + platform_analytics), asks the model for a revised strategy as a
PROPOSAL with the evidence behind every change, and tells the operator on Telegram. Nothing changes until the
operator applies it; thin data means "keep the course", never a guess.

    month_results(client_id, month, db_path)        -> {platform: {posts: [...], measured, baseline}}
    propose_review(client_id, month, *, db_path, llm) -> proposal dict (stored as brief["strategy_proposal"])
    apply_proposal(client_id, db_path) / discard_proposal(client_id, db_path) -> bool
    run_due_reviews(db_path, today, llm)            -> [client_id]   previous-month reviews not done yet
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="client_review")

MIN_MEASURED = 3
_REVIEW_MAX_TOKENS = 4000


def _p(db_path):
    return Path(db_path) if db_path else None


def _metrics_sql() -> str:
    return ("SELECT ci.platform, ci.published_at, substr(COALESCE(ci.caption, ci.title, ''), 1, 140) AS opening, "
            "pa.likes, pa.comments, pa.impressions, pa.views, pa.engagement_rate "
            "FROM content_items ci LEFT JOIN platform_analytics pa ON pa.content_id = ci.content_id AND pa.snapshot_date = "
            "(SELECT MAX(snapshot_date) FROM platform_analytics WHERE content_id = ci.content_id) "
            "WHERE ci.client_id = ? AND substr(ci.published_at, 1, 10) >= ? AND substr(ci.published_at, 1, 10) < ? ORDER BY ci.published_at")


def _summ(rows: list) -> dict:
    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    return {"posts": len(rows), "avg_likes": avg("likes"), "avg_comments": avg("comments"),
            "avg_impressions": avg("impressions"), "avg_engagement_rate_pct": avg("engagement_rate")}


def month_results(client_id: str, month: str, db_path=None) -> dict:
    from hypogeum.db import get_connection  # noqa: PLC0415
    y, m = (int(x) for x in month.split("-"))
    start, nxt = date(y, m, 1), (date(y, m, 1) + timedelta(days=32)).replace(day=1)
    base_start = start - timedelta(days=90)
    conn = get_connection(_p(db_path))
    try:
        last = [dict(r) for r in conn.execute(_metrics_sql(), (client_id, start.isoformat(), nxt.isoformat()))]
        base = [dict(r) for r in conn.execute(_metrics_sql(), (client_id, base_start.isoformat(), start.isoformat()))]
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    out = {}
    for plat in sorted({r["platform"] for r in last + base}):
        mine = [r for r in last if r["platform"] == plat]
        out[plat] = {"posts": [{"date": (r["published_at"] or "")[:10], "opening": r["opening"], "likes": r["likes"], "comments": r["comments"],
                                "impressions": r["impressions"], "engagement_rate_pct": r["engagement_rate"]} for r in mine][:20],
                     "last_month": _summ(mine), "baseline_90d": _summ([r for r in base if r["platform"] == plat]),
                     "measured": sum(1 for r in mine if any(r.get(k) is not None for k in ("likes", "comments", "impressions")))}
    return out


def _calendar_summary(brief: dict, month: str) -> dict:
    from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
    posts = (calendars_of(brief).get(month) or {}).get("posts") or []
    by_status = {}
    for p in posts:
        by_status[p.get("status") or "draft"] = by_status.get(p.get("status") or "draft", 0) + 1
    return {"planned": len(posts), "by_status": by_status,
            "pillars_published": sorted({p.get("pillar") for p in posts if (p.get("status") == "published") and p.get("pillar")})}


def propose_review(client_id: str, month: str, *, db_path=None, llm: Optional[Callable] = None) -> dict:
    from hypogeum import client_manager as cm  # noqa: PLC0415
    from hypogeum.agents import brand_strategy_agent as bsa  # noqa: PLC0415
    from hypogeum.prompt_loader import load_prompt  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path))
    if not client:
        raise bsa.BrandGenerationError(f"no client {client_id!r}")
    brief = dict(client.get("brief") or {})
    current = brief.get("brand_strategy")
    if not current:
        raise bsa.BrandGenerationError("there is no strategy to review")
    bsa._check_budget(client, db_path)
    results = month_results(client_id, month, db_path)
    measured = sum(v["measured"] for v in results.values())
    ctx = bsa.build_context(client_id, db_path=_p(db_path))
    payload = {"profile": ctx["profile"], "current_strategy": current, "month": month, "last_month_results": results,
               "calendar": _calendar_summary(brief, month), "thin_data": measured < MIN_MEASURED}
    llm = llm or bsa._default_llm()
    cost = 0.0
    try:
        raw, cost = llm(load_prompt(bsa._PROMPTS / "brand_review.txt"), "Input:\n" + json.dumps(payload, ensure_ascii=False, default=str),
                        _REVIEW_MAX_TOKENS, agent="brand_review_agent", client_id=client_id)
        data = bsa._parse(raw)
        strategy = bsa._coerce_strategy(data.get("strategy") or {}, ctx)
    except Exception as exc:  # noqa: BLE001
        bsa._log_call(brief, f"review {month}", cost, False, str(exc))
        cm.update_client(client_id, brief=brief, db_path=_p(db_path))
        raise bsa.BrandGenerationError(f"review failed: {exc}") from exc
    changes = [{k: str(c.get(k) or "") for k in ("what", "why", "evidence")} for c in (data.get("changes") or []) if isinstance(c, dict) and c.get("what")][:6]
    if payload["thin_data"]:
        changes = [c for c in changes if c["evidence"]]                 # on thin data only evidence-backed changes survive
    proposal = {"status": "pending", "month": month, "created_at": datetime.now(timezone.utc).isoformat(),
                "thin_data": payload["thin_data"], "measured_posts": measured, "summary": str(data.get("summary") or "")[:600],
                "changes": changes, "keep": [str(k) for k in (data.get("keep") or []) if str(k).strip()][:5], "strategy": strategy,
                "based_on_version": current.get("version")}
    brief["strategy_proposal"] = proposal
    brief.setdefault("reviews", {})[month] = {"status": "pending", "at": proposal["created_at"]}
    bsa._log_call(brief, f"review {month}", cost, True)
    cm.update_client(client_id, brief=brief, db_path=_p(db_path))
    log.info("client_review_proposed", client_id=client_id, month=month, changes=len(changes), cost_usd=cost)
    return proposal


def _decide(client_id: str, db_path, apply: bool) -> bool:
    from hypogeum import client_manager as cm  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path))
    brief = dict((client or {}).get("brief") or {})
    prop = brief.get("strategy_proposal")
    if not prop or prop.get("status") != "pending":
        return False
    current = brief.get("brand_strategy") or {}
    if apply:
        if prop.get("based_on_version") != current.get("version"):
            return False                                                   # the strategy changed after the proposal: stale
        brief.setdefault("brand_strategy_history", []).append(current)
        new = dict(prop["strategy"])
        new.update(version=int(current.get("version") or 0) + 1, created_at=datetime.now(timezone.utc).date().isoformat(),
                   author=f"Cortex (monthly review {prop['month']}, approved by the operator)")
        brief["brand_strategy"] = new
    brief.setdefault("reviews", {})[prop["month"]] = {"status": "applied" if apply else "discarded", "at": datetime.now(timezone.utc).isoformat()}
    brief.pop("strategy_proposal", None)
    return bool(cm.update_client(client_id, brief=brief, db_path=_p(db_path)))


def month_of_last_decision(client_id: str, db_path=None) -> str:
    """The month of the review decided most recently (to close its Telegram alert)."""
    from hypogeum import client_manager as cm  # noqa: PLC0415
    reviews = ((cm.get_client(client_id, db_path=_p(db_path)) or {}).get("brief") or {}).get("reviews") or {}
    decided = sorted((v.get("at", ""), m) for m, v in reviews.items() if v.get("status") in ("applied", "discarded"))
    return decided[-1][1] if decided else ""


def apply_proposal(client_id: str, db_path=None) -> bool:
    return _decide(client_id, db_path, True)


def discard_proposal(client_id: str, db_path=None) -> bool:
    return _decide(client_id, db_path, False)


def run_due_reviews(db_path=None, today: Optional[date] = None, llm: Optional[Callable] = None) -> list:
    """Review LAST month for every active client that has a strategy, answered the questionnaire, has had the
    strategy for at least two weeks, and has no review of that month yet. One attempt per client per day."""
    from hypogeum import client_manager as cm  # noqa: PLC0415
    today = today or datetime.now(timezone.utc).date()
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    done = []
    for c in cm.list_clients(db_path=_p(db_path)):
        if (c.get("status") or "active") != "active":
            continue
        brief = c.get("brief") or {}
        strat = brief.get("brand_strategy")
        if not strat or prev in (brief.get("reviews") or {}):
            continue
        try:
            if (today - date.fromisoformat(str(strat.get("created_at"))[:10])).days < 14:
                continue
        except ValueError:
            continue
        if (brief.get("review_attempt") or "") == today.isoformat():
            continue
        brief = dict(brief)
        brief["review_attempt"] = today.isoformat()
        cm.update_client(c["client_id"], brief=brief, db_path=_p(db_path))
        try:
            propose_review(c["client_id"], prev, db_path=db_path, llm=llm)
            done.append(c["client_id"])
        except Exception as exc:  # noqa: BLE001 — budget reached, model error: tomorrow is another try
            log.warn("client_review_failed", client_id=c["client_id"], error=str(exc)[:160])
    return done


def start_client_review_loop(db_path=None, interval_s: int = 6 * 3600):
    """Looks for a due review a few times a day (one attempt per client per day); costs nothing when none is due."""
    import threading  # noqa: PLC0415
    stop = threading.Event()

    def _loop():
        stop.wait(120)
        while not stop.is_set():
            try:
                run_due_reviews(db_path)
            except Exception as exc:  # noqa: BLE001
                log.warn("client_review_loop_failed", error=str(exc)[:200])
            stop.wait(interval_s)

    t = threading.Thread(target=_loop, name="client-review", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("client_review_loop_started")
    return t
