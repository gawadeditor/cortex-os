"""
hypogeum/agents/brand_strategy_agent.py — turn a client's questionnaire answers
and real platform data into (1) a personal-brand strategy and (2) a month of
ready-to-review post drafts, stored in clients.brief:

    brand_profile      (input, from the dashboard questionnaire)
    brand_strategy     (output 1, versioned; previous versions kept in
                        brand_strategy_history)
    content_calendar   (output 2, one month; previous month kept in
                        content_calendar_history)
    generation_log     (every LLM call: what, model, cost, ok/error)

Post DATES are computed here, deterministically, from posts/week — the model
only writes the content for each slot. Every call is cost-gated by the
client's monthly budget (shared with content analysis) and goes through
hypogeum.openrouter.call_claude like every other Claude call. Like
content_analysis_agent, outputs are validated by a local _coerce (they are
client documents, not pipeline schemas).
"""
from __future__ import annotations

import calendar as _cal
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger
from hypogeum.prompt_loader import load_prompt

log = get_logger(agent="brand_strategy_agent")

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_MODEL = "anthropic/claude-sonnet-4.6"
_STRATEGY_MAX_TOKENS = 3000
_CALENDAR_MAX_TOKENS = 9000
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)
_PLATFORMS = ("linkedin", "instagram", "facebook", "youtube", "tiktok")

# posts/week -> weekdays (Mon=0). Tue/Thu are the strongest professional days.
_WEEKDAYS = {1: [1], 2: [1, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 3, 4],
             6: [0, 1, 2, 3, 4, 6], 7: [0, 1, 2, 3, 4, 5, 6]}


class BrandGenerationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse(raw: str) -> dict:
    s = (raw or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
    return json.loads(s)


def _default_llm(config=None) -> Callable[[str, str, int], tuple]:
    from hypogeum.openrouter import call_claude
    if config is None:
        from hypogeum.config import Config
        config = Config()

    def _run(system: str, user: str, max_tokens: int, *, agent: str, client_id: str) -> tuple:
        res = call_claude(model_slug=_MODEL,
                          messages=[{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                          max_tokens=max_tokens, config=config, agent=agent,
                          episode_id=f"client:{client_id}")
        return res.content, float(getattr(res, "estimated_cost_usd", 0.0) or 0.0)
    return _run


def generation_spend_this_month(brief: dict) -> float:
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum(float(e.get("cost_usd") or 0.0) for e in (brief.get("generation_log") or [])
               if str(e.get("at", "")).startswith(prefix))


def _check_budget(client: dict, db_path) -> None:
    from hypogeum import client_manager as cm
    budget = float(client.get("monthly_analysis_budget_usd") or 0.0)
    if not budget:
        return
    spent = cm.analysis_spend_this_month(client["client_id"], db_path=db_path) \
        + generation_spend_this_month(client.get("brief") or {})
    if spent >= budget:
        raise BrandGenerationError(
            f"monthly budget reached (${spent:.2f} of ${budget:.2f}) — raise it in Accounts → Client settings")


def _log_call(brief: dict, what: str, cost: float, ok: bool, error: str = "") -> None:
    brief.setdefault("generation_log", []).append({
        "at": datetime.now(timezone.utc).isoformat(), "what": what, "model": _MODEL,
        "cost_usd": round(float(cost or 0.0), 4), "ok": ok, "error": error[:300]})


def build_context(client_id: str, *, db_path: Optional[Path] = None) -> dict:
    """Everything the model may use — and nothing else."""
    from hypogeum import client_manager as cm
    from hypogeum.client_advisory import generate_platform_advice
    from hypogeum.dashboard_clients import get_client_linkedin_snapshot

    client = cm.get_client(client_id, db_path=db_path) or {}
    brief = client.get("brief") or {}
    ctx = {"name": client.get("name"), "profile": brief.get("brand_profile") or {},
           "platforms_connected": sorted({a["platform"] for a in
                                          cm.list_managed_accounts(client_id, db_path=db_path)})}
    try:
        adv = generate_platform_advice(client_id, db_path=db_path)
        ctx["platform_data"] = {p: {k: v[k] for k in ("posts", "last_post", "posts_365d", "findings", "audience")}
                                for p, v in adv.items()}
    except Exception as exc:  # noqa: BLE001
        log.warn("brand_context_advice_failed", client_id=client_id, error=str(exc))
    snap = get_client_linkedin_snapshot(client_id, db_path=db_path)
    if snap:
        ctx["linkedin_export"] = {k: snap[k] for k in ("followers_total", "impressions", "members_reached",
                                                       "new_followers", "audience")}
    return ctx


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

def _coerce_strategy(s: dict, ctx: dict) -> dict:
    out = {}
    out["positioning"] = str(s.get("positioning") or "").strip()
    if not out["positioning"]:
        raise BrandGenerationError("strategy has no positioning")
    pillars = []
    for p in (s.get("pillars") or [])[:5]:
        if isinstance(p, dict) and p.get("name"):
            pillars.append({"name": str(p["name"]).strip(), "why": str(p.get("why") or "").strip(),
                            "formats": [str(f) for f in (p.get("formats") or []) if f][:4]})
    if len(pillars) < 2:
        raise BrandGenerationError("strategy needs at least 2 pillars")
    out["pillars"] = pillars
    connected = set(ctx.get("platforms_connected") or [])
    plats = {}
    for name, v in (s.get("platforms") or {}).items():
        key = str(name).lower()
        if key in _PLATFORMS and (not connected or key in connected) and isinstance(v, dict):
            plats[key] = {"role": str(v.get("role") or ""), "language": str(v.get("language") or "")}
    out["platforms"] = plats
    cad = s.get("cadence") or {}
    ppw = ctx.get("profile", {}).get("posts_per_week") or cad.get("posts_per_week") or 2
    try:
        ppw = max(1, min(7, int(ppw)))
    except (TypeError, ValueError):
        ppw = 2
    out["cadence"] = {"posts_per_week": ppw, "plan": str(cad.get("plan") or "")}
    for k in ("kpis", "guardrails", "evidence", "open_questions"):
        out[k] = [str(x) for x in (s.get(k) or []) if str(x).strip()][:8]
    out["cta"] = str(s.get("cta") or "")
    return out


def generate_strategy(client_id: str, *, db_path: Optional[Path] = None,
                      llm: Optional[Callable] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        raise BrandGenerationError(f"no client {client_id!r}")
    brief = dict(client.get("brief") or {})
    if not (brief.get("brand_profile") or {}).get("answered_at"):
        raise BrandGenerationError("answer the questionnaire first")
    _check_budget(client, db_path)
    ctx = build_context(client_id, db_path=db_path)
    llm = llm or _default_llm()
    cost = 0.0
    try:
        raw, cost = llm(load_prompt(_PROMPTS / "brand_strategy.txt"),
                        "Input:\n" + json.dumps(ctx, ensure_ascii=False, default=str),
                        _STRATEGY_MAX_TOKENS, agent="brand_strategy_agent", client_id=client_id)
        strategy = _coerce_strategy(_parse(raw), ctx)
    except Exception as exc:  # noqa: BLE001
        _log_call(brief, "strategy", cost, False, str(exc))
        cm.update_client(client_id, brief=brief, db_path=db_path)
        raise BrandGenerationError(f"strategy generation failed: {exc}") from exc
    prev = brief.get("brand_strategy")
    if prev:
        brief.setdefault("brand_strategy_history", []).append(prev)
    strategy.update(version=int((prev or {}).get("version") or 0) + 1,
                    created_at=datetime.now(timezone.utc).date().isoformat(),
                    author=f"Cortex (auto, {_MODEL})")
    brief["brand_strategy"] = strategy
    _log_call(brief, "strategy", cost, True)
    cm.update_client(client_id, brief=brief, db_path=db_path)
    log.info("brand_strategy_generated", client_id=client_id, version=strategy["version"], cost_usd=cost)
    return strategy


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------

def month_slots(month: str, posts_per_week: int, platforms: list,
                *, today: Optional[date] = None) -> list:
    """[{date, day, allowed_platforms}] for the given 'YYYY-MM', on the
    weekdays for posts_per_week, skipping days already past."""
    y, m = (int(x) for x in month.split("-"))
    days = _WEEKDAYS.get(max(1, min(7, posts_per_week)), [1, 3])
    today = today or datetime.now(timezone.utc).date()
    out = []
    for d in range(1, _cal.monthrange(y, m)[1] + 1):
        dt = date(y, m, d)
        if dt.weekday() in days and dt > today:
            out.append({"date": dt.isoformat(), "day": dt.strftime("%a"), "allowed_platforms": list(platforms)})
    return out


def next_month(today: Optional[date] = None) -> str:
    t = today or datetime.now(timezone.utc).date()
    first_next = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next.strftime("%Y-%m")


def _coerce_calendar(data: dict, slots: list, lang_default: str) -> list:
    by_date = {p.get("date"): p for p in (data.get("posts") or []) if isinstance(p, dict)}
    posts = []
    for s in slots:
        p = by_date.get(s["date"])
        if not p or not str(p.get("draft") or "").strip():
            continue
        plats = [x for x in (p.get("platforms") or []) if x in s["allowed_platforms"]] or s["allowed_platforms"][:1]
        posts.append({"date": s["date"], "day": s["day"], "platforms": plats,
                      "lang": p.get("lang") if p.get("lang") in ("en", "ar") else lang_default,
                      "pillar": str(p.get("pillar") or ""), "format": str(p.get("format") or ""),
                      "needs": (str(p["needs"]) if p.get("needs") else None),
                      "draft": str(p["draft"]).strip(), "status": "draft"})
    if not posts:
        raise BrandGenerationError("the model returned no usable posts")
    return posts


def generate_calendar(client_id: str, *, month: Optional[str] = None, db_path: Optional[Path] = None,
                      llm: Optional[Callable] = None, today: Optional[date] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        raise BrandGenerationError(f"no client {client_id!r}")
    brief = dict(client.get("brief") or {})
    strategy = brief.get("brand_strategy")
    if not strategy:
        raise BrandGenerationError("generate a strategy first")
    _check_budget(client, db_path)
    month = month or next_month(today)
    ctx = build_context(client_id, db_path=db_path)
    platforms = [p for p in (strategy.get("platforms") or {}) if p in ctx["platforms_connected"]] \
        or ctx["platforms_connected"] or ["linkedin"]
    ppw = int((strategy.get("cadence") or {}).get("posts_per_week") or 2)
    slots = month_slots(month, ppw, platforms, today=today)
    if not slots:
        raise BrandGenerationError(f"no posting days left in {month}")
    langs = (ctx["profile"].get("languages") or ["en"])
    lang_default = "ar" if langs == ["ar"] else "en"
    llm = llm or _default_llm()
    payload = {"profile": ctx["profile"], "strategy": strategy, "platform_data": ctx.get("platform_data"),
               "post_language": {"en": "English", "ar": "Arabic"}[lang_default] if len(langs) == 1
               else "alternate English and Arabic", "slots": slots}
    cost = 0.0
    try:
        raw, cost = llm(load_prompt(_PROMPTS / "brand_calendar.txt"),
                        "Input:\n" + json.dumps(payload, ensure_ascii=False, default=str),
                        _CALENDAR_MAX_TOKENS, agent="brand_calendar_agent", client_id=client_id)
        posts = _coerce_calendar(_parse(raw), slots, lang_default)
    except Exception as exc:  # noqa: BLE001
        _log_call(brief, f"calendar {month}", cost, False, str(exc))
        cm.update_client(client_id, brief=brief, db_path=db_path)
        raise BrandGenerationError(f"calendar generation failed: {exc}") from exc
    from hypogeum.dashboard_clients import calendars_of
    cals = calendars_of(brief)
    prev = cals.get(month)
    if prev and prev.get("posts"):
        brief.setdefault("content_calendar_history", []).append(prev)
    cal = {"month": month, "status": "draft_for_review", "generated_at": datetime.now(timezone.utc).isoformat(),
           "strategy_version": strategy.get("version"), "posts": posts}
    cals[month] = cal
    brief["content_calendars"] = cals
    brief.pop("content_calendar", None)
    _log_call(brief, f"calendar {month}", cost, True)
    cm.update_client(client_id, brief=brief, db_path=db_path)
    log.info("brand_calendar_generated", client_id=client_id, month=month, posts=len(posts), cost_usd=cost)
    return cal


# ---------------------------------------------------------------------------
# Rolling drafts (operator decision 2026-10-03): keep only N drafts ahead
# (default 2) instead of a whole month; each post marked as posted triggers
# one new draft — the same buffer idea the factory uses for episodes.
# ---------------------------------------------------------------------------

DRAFTS_AHEAD = 2


def upcoming_slots(after: date, n: int, posts_per_week: int, platforms: list) -> list:
    """The next n posting days strictly after `after`, on the cadence weekdays,
    crossing month boundaries as needed."""
    days = _WEEKDAYS.get(max(1, min(7, posts_per_week)), [1, 3])
    out, d = [], after
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() in days:
            out.append({"date": d.isoformat(), "day": d.strftime("%a"), "allowed_platforms": list(platforms)})
    return out


def _all_posts(brief: dict) -> list:
    from hypogeum.dashboard_clients import calendars_of
    return [p for cal in calendars_of(brief).values() for p in (cal.get("posts") or [])]


def draft_next_posts(client_id: str, *, db_path: Optional[Path] = None, llm: Optional[Callable] = None,
                     today: Optional[date] = None, ahead: Optional[int] = None) -> list:
    """Top the queue of unposted upcoming drafts back up to `ahead` (default
    DRAFTS_AHEAD). Returns the new posts ([] when already full)."""
    from hypogeum import client_manager as cm
    from hypogeum.dashboard_clients import calendars_of
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        raise BrandGenerationError(f"no client {client_id!r}")
    brief = dict(client.get("brief") or {})
    strategy = brief.get("brand_strategy")
    if not strategy:
        raise BrandGenerationError("generate a strategy first")
    today = today or datetime.now(timezone.utc).date()
    prof = brief.get("brand_profile") or {}
    if ahead is None and prof.get("drafts_paused"):
        return []   # operator paused drafting for this client
    ahead = int(ahead if ahead is not None else (prof.get("drafts_ahead") or DRAFTS_AHEAD))
    posts = _all_posts(brief)
    pending = [p for p in posts if (p.get("status") or "draft") in ("draft", "approved")
               and p.get("date", "") > today.isoformat()]
    need = ahead - len(pending)
    if need <= 0:
        return []
    _check_budget(client, db_path)
    ctx = build_context(client_id, db_path=db_path)
    platforms = [p for p in (strategy.get("platforms") or {}) if p in ctx["platforms_connected"]] \
        or ctx["platforms_connected"] or ["linkedin"]
    ppw = int((strategy.get("cadence") or {}).get("posts_per_week") or 2)
    last = max([date.fromisoformat(p["date"]) for p in posts if p.get("date")] + [today])
    slots = upcoming_slots(last, need, ppw, platforms)
    langs = ctx["profile"].get("languages") or ["en"]
    lang_default = "ar" if langs == ["ar"] else "en"
    previous = sorted(posts, key=lambda p: p.get("date", ""))[-40:]
    payload = {"profile": ctx["profile"], "strategy": strategy, "platform_data": ctx.get("platform_data"),
               "post_language": {"en": "English", "ar": "Arabic"}[lang_default] if len(langs) == 1
               else "alternate English and Arabic",
               "previous_posts": [{"date": p.get("date"), "pillar": p.get("pillar"),
                                   "first_line": (p.get("draft") or "").split("\n", 1)[0]}
                                  for p in previous],
               "allowed_names": list((ctx["profile"].get("allowed_names") or [])),
               "slots": slots}
    llm = llm or _default_llm()
    cost = 0.0
    try:
        raw, cost = llm(load_prompt(_PROMPTS / "brand_calendar.txt"),
                        "Input:\n" + json.dumps(payload, ensure_ascii=False, default=str),
                        _STRATEGY_MAX_TOKENS + 1500, agent="brand_calendar_agent", client_id=client_id)
        new = _coerce_calendar(_parse(raw), slots, lang_default)
    except Exception as exc:  # noqa: BLE001
        _log_call(brief, f"drafts x{need}", cost, False, str(exc))
        cm.update_client(client_id, brief=brief, db_path=db_path)
        raise BrandGenerationError(f"drafting failed: {exc}") from exc
    cals = calendars_of(brief)
    for p in new:
        m = p["date"][:7]
        cal = dict(cals.get(m) or {"month": m, "status": "rolling", "posts": []})
        cal["posts"] = sorted(list(cal.get("posts") or []) + [p], key=lambda x: x.get("date", ""))
        cal["strategy_version"] = strategy.get("version")
        cals[m] = cal
    brief["content_calendars"] = cals
    brief.pop("content_calendar", None)
    _log_call(brief, f"drafts x{len(new)}", cost, True)
    cm.update_client(client_id, brief=brief, db_path=db_path)
    log.info("brand_drafts_generated", client_id=client_id, count=len(new), cost_usd=cost)
    return new
