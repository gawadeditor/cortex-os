"""
hypogeum/channel_plan.py — from "what do I want?" to a concrete, evidence-backed plan for a new channel.

The first thing a person starting a channel should NOT have to do is describe an idea in their own words. They pick a goal
and answer a few multiple-choice questions; the factory then researches the market itself — YouTube channels created in the
last year that already hold millions of views in the matching niches, and HOW they work (when they publish, how long their
videos are, the title skeletons they reuse, how the description is written, which hashtags, how fast they grew) — and turns
that into a plan: the best niche options with their evidence, a blueprint (format, cadence, titles, description, hashtags,
proven topics), the first 30 days, what it costs the factory to run, and whether the monetisation threshold is realistic.
The plan pre-fills the Channel Builder, so nothing has to be typed to start.

    QUESTIONS                                   the questionnaire (rendered by the page)
    start_plan(answers, db_path)  -> plan_id     validates, stores, runs the research in a background thread
    get_plan(plan_id, db_path)    -> dict        {status, progress, answers, plan, error}
    build_plan(answers, report, cost_per_episode) -> dict    pure: options + blueprint + prefill

Never spends money: only the YouTube Data API's free daily quota (about 2,000 of 10,000 units per plan).
Lock rule: all HTTP with no database connection open.
"""
from __future__ import annotations

import json
import statistics
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="channel_plan")

# ---------------------------------------------------------------------------
# the questionnaire — choices only, nothing to type
# ---------------------------------------------------------------------------

FAMILIES: Dict[str, dict] = {
    "history":    {"label": "History, archaeology & mysteries", "rpm": "medium", "kids": False,
                   "seeds": ["ancient mysteries", "history facts", "unsolved mysteries", "archaeology", "ancient history", "dark history"]},
    "finance":    {"label": "Money, investing & business", "rpm": "high", "kids": False,
                   "seeds": ["personal finance tips", "investing for beginners", "side hustle ideas", "small business tips"]},
    "facts":      {"label": "Science, space & facts", "rpm": "medium", "kids": False,
                   "seeds": ["space facts", "science explained", "psychology facts", "geography facts", "animal facts", "dinosaur facts"]},
    "motivation": {"label": "Motivation & self-improvement", "rpm": "medium", "kids": False,
                   "seeds": ["stoicism motivation", "inspiring true stories", "self improvement tips", "morning routine"]},
    "lifestyle":  {"label": "Lifestyle, health & food", "rpm": "medium", "kids": False,
                   "seeds": ["fitness tips", "cooking shorts", "life hacks", "luxury lifestyle", "healthy habits"]},
    "stories":    {"label": "Stories, horror & entertainment", "rpm": "low", "kids": False,
                   "seeds": ["AI generated story", "scary horror stories", "what if scenarios", "movie recap", "true crime stories"]},
    "kids":       {"label": "Kids & family", "rpm": "low", "kids": True,
                   "seeds": ["bedtime story for kids", "kids cartoon story", "educational kids videos", "relaxing sleep story"]},
    "tech":       {"label": "Tech & AI", "rpm": "high", "kids": False,
                   "seeds": ["AI tools tutorial", "tech news explained", "productivity apps", "chatgpt tips"]},
}
_RPM_WEIGHT = {"high": 1.4, "medium": 1.0, "low": 0.65}
_RPM_TEXT = {"high": "advertisers pay the most in this area (high CPM)", "medium": "advertiser rates are average",
             "low": "advertiser rates are low here, so revenue per view is small and volume matters"}

QUESTIONS: List[dict] = [
    {"id": "goal", "title": "What do you want this channel to do for you?", "type": "single", "options": [
        {"id": "income", "label": "Make money", "hint": "Ad revenue first, then offers — we pick what pays best"},
        {"id": "audience", "label": "Grow a big audience fast", "hint": "Reach first, monetise later"},
        {"id": "business", "label": "Bring customers to my business or brand", "hint": "Attention that converts"},
        {"id": "authority", "label": "Become a recognised voice in a field", "hint": "Credibility and a following"},
        {"id": "specific", "label": "Something specific I have in mind", "hint": "You choose the area, we research it"}]},
    {"id": "families", "title": "Which areas are you happy to make content in?", "type": "multi", "hint": "Pick several, or leave all and let the research decide.",
     "options": [{"id": k, "label": v["label"]} for k, v in FAMILIES.items()]},
    {"id": "language", "title": "Which language will the channel speak?", "type": "single", "options": [
        {"id": "en", "label": "English"}, {"id": "ar", "label": "Arabic"}, {"id": "es", "label": "Spanish"},
        {"id": "fr", "label": "French"}, {"id": "pt", "label": "Portuguese"}, {"id": "hi", "label": "Hindi"}]},
    {"id": "audience", "title": "Who should watch?", "type": "single", "options": [
        {"id": "everyone", "label": "Everyone"}, {"id": "kids", "label": "Kids and parents"}, {"id": "young", "label": "Young adults (18–34)"},
        {"id": "adults", "label": "Adults (35+)"}, {"id": "seniors", "label": "Older audience (55+)"}]},
    {"id": "format", "title": "What format?", "type": "single", "options": [
        {"id": "shorts", "label": "Short videos (under 3 minutes)", "hint": "Fastest growth, lowest cost per video"},
        {"id": "long", "label": "Longer videos", "hint": "Slower, higher revenue per view"},
        {"id": "both", "label": "A mix of both"}]},
    {"id": "effort", "title": "How much should it publish?", "type": "single", "options": [
        {"id": "light", "label": "About 3 videos a week", "hint": "Lowest cost"},
        {"id": "steady", "label": "About 7 videos a week", "hint": "One a day"},
        {"id": "heavy", "label": "About 14 videos a week", "hint": "Two a day — fastest learning, highest cost"}]},
    {"id": "risk", "title": "How adventurous should the niche be?", "type": "single", "options": [
        {"id": "proven", "label": "Proven formula", "hint": "Many new channels already won here"},
        {"id": "balanced", "label": "Balanced"},
        {"id": "gap", "label": "A gap with less competition", "hint": "Fewer winners, bigger upside, more uncertainty"}]},
    {"id": "money", "title": "How do you plan to earn from it?", "type": "single", "options": [
        {"id": "ads", "label": "YouTube ad revenue"}, {"id": "affiliate", "label": "Affiliate links and products"},
        {"id": "sponsors", "label": "Sponsorships"}, {"id": "unsure", "label": "Not sure yet"}]},
]
_VALID = {q["id"]: {o["id"] for o in q["options"]} for q in QUESTIONS}
_CADENCE = {"light": 3, "steady": 7, "heavy": 14}


def validate(answers: dict) -> Optional[str]:
    for q in QUESTIONS:
        v = answers.get(q["id"])
        if q["type"] == "single":
            if v not in _VALID[q["id"]]:
                return f"choose an option for: {q['title']}"
        else:
            if v is not None and (not isinstance(v, list) or any(x not in _VALID[q["id"]] for x in v)):
                return f"invalid choice for: {q['title']}"
    return None


def seeds_for(answers: dict, *, limit: int = 18) -> List[str]:
    fams = [f for f in (answers.get("families") or []) if f in FAMILIES] or list(FAMILIES)
    if answers.get("audience") != "kids" and answers.get("goal") != "specific":
        fams = [f for f in fams if not FAMILIES[f]["kids"]] or fams
    per = max(2, limit // max(len(fams), 1))
    out: List[str] = []
    for f in fams:
        out += FAMILIES[f]["seeds"][:per]
    return list(dict.fromkeys(out))[:limit]


def _family_of(niche: str) -> Optional[str]:
    for k, f in FAMILIES.items():
        if niche in f["seeds"]:
            return k
    return None


# ---------------------------------------------------------------------------
# the plan (pure)
# ---------------------------------------------------------------------------

def _score(n: dict, answers: dict) -> float:
    fam = _family_of(n["niche"])
    rpm = _RPM_WEIGHT[FAMILIES[fam]["rpm"]] if fam else 1.0
    evidence = min(n["channels"], 10) / 10
    reach = min(n["median_views_per_video"] / 100_000, 3.0)
    competition = 1.0 - min(n["channels"], 12) / 14            # fewer winners = more gap (also fewer proofs)
    goal, risk = answers.get("goal"), answers.get("risk")
    s = {"income": rpm * (0.5 + reach) + evidence, "audience": 1.5 * reach + evidence, "business": rpm + evidence + (0.5 if fam else 0),
         "authority": evidence + rpm * 0.5, "specific": evidence + reach}.get(goal, evidence + reach)
    s += {"proven": 1.5 * evidence, "balanced": 0.5 * evidence + 0.5 * competition, "gap": 1.5 * competition + 0.5 * reach}.get(risk, 0)
    if n["channels"] < 3:
        s -= 1.5                                # two lucky channels are an anecdote, not a pattern
    fmt = answers.get("format")
    if fmt == "shorts" and n["shorts_share"] < 0.5:
        s -= 1.2
    if fmt == "long" and n["shorts_share"] > 0.5:
        s -= 1.2
    if answers.get("families") and fam and fam not in answers["families"]:
        s -= 2.0
    return s


def _competition_level(n: dict) -> str:
    return "crowded" if n["channels"] >= 8 else ("active" if n["channels"] >= 4 else "thin")


def _milestone(uploads_per_week: float, median_views: int) -> dict:
    """The YouTube Partner Programme gate for Shorts-first channels: 1,000 subscribers and 10M Shorts views in 90 days (or 4,000 watch hours)."""
    ninety = int(uploads_per_week * 13 * median_views)
    return {"projected_views_90d": ninety, "ypp_shorts_views_needed": 10_000_000,
            "verdict": ("realistic at the proven pace" if ninety >= 10_000_000 else
                        "reachable only if the channel beats the median proven channel" if ninety >= 3_000_000 else
                        "unlikely inside 90 days at this pace — plan on the 4,000-watch-hour route or a higher cadence")}


def _blueprint(n: dict, answers: dict, cost_per_episode: float) -> dict:
    how = n.get("how") or {}
    t, d = n["title"], n["description"]
    per_week = _CADENCE.get(answers.get("effort"), 7)
    fmt = answers.get("format") or ("shorts" if n["shorts_share"] >= 0.5 else "long")
    cost_month = round(per_week * 4.3 * cost_per_episode)
    hours = how.get("best_hours_utc") or []
    templates = [x for x in (how.get("title_templates") or [])][:4]
    top_videos = (n.get("examples") or [])[:5]
    plan30 = [
        "Week 1 — set up the channel and handles, publish the first %d videos using the title formulas below, no more than %d a day." % (min(per_week, 7), 2),
        "Week 2 — keep the proven rhythm (%s uploads a week). Read the first retention curves; the factory flags where viewers leave." % per_week,
        "Week 3 — double down on the 2–3 topics that beat the median; drop what did not. Compare views to the niche benchmark (%s per video)." % f"{n['median_views_per_video']:,}",
        "Week 4 — review: subscribers, views per video, search terms people used. The factory rewrites the next scripts from what it measured.",
    ]
    return {
        "format": fmt, "uploads_per_week": per_week, "proven_uploads_per_week": n["uploads_per_week"],
        "post_at_utc": [f"{h:02d}:00" for h in hours], "post_at_uae": [f"{(h + 4) % 24:02d}:00" for h in hours], "best_days": how.get("best_days") or [],
        "video_length_s": how.get("median_length_s"),
        "titles": {"length": t["avg_len"], "numbers_pct": t["number_pct"], "questions_pct": t["question_pct"], "power_words_pct": t["power_word_pct"],
                   "openers": [w for w, _ in t["top_openers"][:5]], "terms": t["top_terms"][:10], "templates": templates},
        "description": {"length": d["avg_len"], "hashtags_each": d["avg_hashtags"], "with_hashtags_pct": d["with_hashtags_pct"],
                        "cta_pct": how.get("cta_pct"), "link_pct": d["with_link_pct"]},
        "hashtags": [h for h, _ in n["hashtags"][:10]], "tags": [t_ for t_, _ in n["tags"][:10]],
        "proven_topics": [{"title": e["title"], "views": e["views"], "channel": e["channel"]} for e in top_videos],
        "first_30_days": plan30,
        "cost": {"per_episode_usd": round(cost_per_episode, 2), "per_month_usd": cost_month,
                 "note": "what it costs the factory to produce, from this factory's own recent episodes"},
        "monetisation": _milestone(per_week, n["median_views_per_video"]),
    }


def _prefill(n: dict, answers: dict, bp: dict) -> dict:
    fam = _family_of(n["niche"])
    aud = {"kids": "Kids and their parents", "young": "Young adults 18–34", "adults": "Adults 35+", "seniors": "Viewers 55 and older", "everyone": "A broad general audience"}.get(
        answers.get("audience"), "A broad general audience")
    tone = "curious, direct, question-led" if bp["titles"]["questions_pct"] >= 25 else ("punchy, fact-first" if bp["titles"]["numbers_pct"] >= 30 else "calm, clear storytelling")
    concept = (f"A faceless, AI-produced channel in the niche \"{n['niche']}\". Videos follow the proven pattern of recent channels that reached millions of views: "
               f"{bp['format']} format, titles of about {bp['titles']['length']} characters, {bp['uploads_per_week']} uploads a week.")
    return {"concept": concept, "genre": n["niche"].title(), "language": answers.get("language", "en"), "format": "shorts" if bp["format"] != "long" else "longform",
            "shorts_per_week": bp["uploads_per_week"], "videos_per_week": 1,
            "brand": {"tone": tone, "target_audience": aud, "content_pillars": bp["titles"]["terms"][:5], "hashtags": bp["hashtags"][:6],
                      "voice_guidelines": "Write titles like the proven channels: " + ", ".join(bp["titles"]["openers"][:3]) + " openers; hook in the first line."},
            "weekly_video_budget_usd": max(60, round(bp["cost"]["per_month_usd"] / 4.3 * 1.3))}


def build_plan(answers: dict, report: dict, cost_per_episode: float = 20.0) -> dict:
    niches = [n for n in report.get("niches", []) if n["channels"] >= 2] or list(report.get("niches", []))
    ranked = sorted(niches, key=lambda n: -_score(n, answers))[:3]
    options = []
    for n in ranked:
        fam = _family_of(n["niche"])
        bp = _blueprint(n, answers, cost_per_episode)
        why = [f"{n['channels']} channels created in the last year already hold a median {n['median_channel_views']:,} views.",
               f"A video there gets a median {n['median_views_per_video']:,} views; the strongest quarter gets {((n.get('how') or {}).get('views_per_video_top25_pct') or 0):,}+.",
               (f"Competition looks {_competition_level(n)} ({n['channels']} proven new channels found)." if _competition_level(n) != "thin" else
                f"Only {n['channels']} proven new channels turned up — either a field with room or a small one; treat the numbers as an early signal.")]
        if fam:
            why.append(f"Money: {_RPM_TEXT[FAMILIES[fam]['rpm']]}." + (" Kids' content is limited by YouTube's made-for-kids rules (no comments, fewer ad types)." if FAMILIES[fam]["kids"] else ""))
        options.append({"niche": n["niche"], "family": fam, "competition": _competition_level(n), "why": why, "blueprint": bp,
                        "niche_stats": {k: n[k] for k in ("niche", "channels", "title", "description", "hashtags", "uploads_per_week", "shorts_share")},
                        "competitors": n.get("channel_examples", []), "how_they_work": n.get("how") or {}, "prefill": _prefill(n, answers, bp), "score": round(_score(n, answers), 2)})
    caveats = ["This shows what worked for channels that already won; many similar channels failed and are not in the data.",
               "Revenue figures are estimates from public numbers, not promises."]
    if not options:
        caveats.insert(0, "The research did not find enough proven new channels for these choices. Try wider areas or another language.")
    return {"options": options, "caveats": caveats, "channels_researched": report.get("channels_total", 0), "videos_read": report.get("videos_total", 0)}


# ---------------------------------------------------------------------------
# run (background)
# ---------------------------------------------------------------------------

def _cost_per_episode(db_path) -> float:
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            rows = [r[0] for r in conn.execute("SELECT total_cost_usd FROM episodes WHERE total_cost_usd > 0 ORDER BY created_at DESC LIMIT 40")]
        finally:
            conn.close()
        return statistics.median(rows) if rows else 20.0
    except Exception:  # noqa: BLE001
        return 20.0


def _save(plan_id: str, db_path, **fields) -> None:
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE channel_plans SET {sets} WHERE plan_id = ?", (*fields.values(), plan_id))
        conn.commit()
    finally:
        conn.close()


def _run(plan_id: str, answers: dict, db_path, max_units: int) -> None:
    from hypogeum import market_intel as mi
    try:
        def progress(stage, i, n, label):
            _save(plan_id, db_path, progress_json=json.dumps({"stage": stage, "i": i, "n": n, "label": label}))

        token, err = mi.get_token(db_path)
        if token is None:
            _save(plan_id, db_path, status="failed", error=err)
            return
        seeds = seeds_for(answers)
        api = mi._Api(token, max_units)
        fx = mi.fetch(api, seeds, language=answers.get("language", "en"), progress=progress)
        progress("analysing", 1, 1, "")
        channels, videos = mi._rows_from_fetch(fx, answers.get("language", "en"))
        report = mi.analyse(channels, videos)
        report["seeds"] = seeds
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            mi._store(conn, channels, videos, report, fx["units"])
            conn.commit()
        finally:
            conn.close()
        plan = build_plan(answers, report, _cost_per_episode(db_path))
        plan["units_used"] = fx["units"]
        plan["stopped"] = fx["stopped"]
        _save(plan_id, db_path, status="done", plan_json=json.dumps(plan, ensure_ascii=False),
              progress_json=json.dumps({"stage": "done", "i": 1, "n": 1, "label": ""}))
    except Exception as exc:  # noqa: BLE001
        log.error("channel_plan_failed", plan_id=plan_id, error=str(exc)[:200])
        try:
            _save(plan_id, db_path, status="failed", error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass


def start_plan(answers: dict, db_path=None, *, max_units: int = 2200, background: bool = True) -> dict:
    err = validate(answers)
    if err:
        return {"ok": False, "error": err}
    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db_path)
    pid = "cp_" + uuid.uuid4().hex[:12]
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO channel_plans (plan_id, created_at, status, answers_json, progress_json) VALUES (?,?,?,?,?)",
                     (pid, datetime.now(timezone.utc).isoformat(), "running", json.dumps(answers), json.dumps({"stage": "starting", "i": 0, "n": 1, "label": ""})))
        conn.commit()
    finally:
        conn.close()
    if background:
        threading.Thread(target=_run, args=(pid, answers, db_path, max_units), name="channel-plan", daemon=True).start()
    else:
        _run(pid, answers, db_path, max_units)
    return {"ok": True, "plan_id": pid}


def get_plan(plan_id: str, db_path=None) -> Optional[dict]:
    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        r = conn.execute("SELECT * FROM channel_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {"plan_id": r["plan_id"], "status": r["status"], "created_at": r["created_at"], "error": r["error"],
            "progress": json.loads(r["progress_json"] or "{}"), "answers": json.loads(r["answers_json"]),
            "plan": json.loads(r["plan_json"]) if r["plan_json"] else None}


# ---------------------------------------------------------------------------
# plan -> the new channel
# ---------------------------------------------------------------------------

def topics_directive_text(option: dict) -> Optional[str]:
    topics = (option.get("blueprint") or {}).get("proven_topics") or []
    if not topics:
        return None
    listing = "; ".join(f'"{t["title"][:90]}" ({t["views"]:,} views)' for t in topics[:6])
    return (f"PROVEN TOPICS IN THIS CHANNEL'S NICHE ({option['niche']}): recent videos from brand-new channels that pulled the most views — {listing}. "
            f"Use them as proof of what this audience wants. Propose NEW subjects and angles in the same territory; never reuse or paraphrase one of these titles.")


def seed_channel_from_plan(channel_id: str, plan_id: str, option_index: int, *, db_path=None) -> dict:
    """The research does not stay in a report: the new channel starts with it. Writes the niche's measured conventions
    (market_playbook, read by the metadata agent) and its proven topics (topic, read by the Topic Ideator) as the channel's
    first learning directives, and links the plan to the channel. Never raises."""
    out = {"seeded": [], "error": None}
    try:
        from hypogeum.channel_learning import record_directive
        from hypogeum.market_intel import market_directive_text
        g = get_plan(plan_id, db_path)
        opts = ((g or {}).get("plan") or {}).get("options") or []
        if not (0 <= option_index < len(opts)):
            out["error"] = "plan option not found"
            return out
        o = opts[option_index]
        stats = o.get("niche_stats")
        if stats:
            record_directive(channel_id, "market_playbook", "prefer", market_directive_text(stats), source_action_id=plan_id, db_path=db_path)
            out["seeded"].append("market_playbook")
        topics = topics_directive_text(o)
        if topics:
            record_directive(channel_id, "topic", "prefer", topics, source_action_id=plan_id, db_path=db_path)
            out["seeded"].append("topic")
        plan = g["plan"]
        plan["created_channel_id"] = channel_id
        plan["created_option"] = option_index
        _save(plan_id, db_path, plan_json=json.dumps(plan, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        log.error("seed_channel_from_plan_failed", channel_id=channel_id, plan_id=plan_id, error=out["error"])
    return out
