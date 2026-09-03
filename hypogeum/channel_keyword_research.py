"""
hypogeum/channel_keyword_research.py — per-channel discovery / SEO intelligence.

A brand-new channel has no name recognition: it is found almost entirely
through generic category, format and audience search. This module does the
homework once per channel and caches it, so every episode's tags and
hashtags can lean on:

  - the hashtags the successful competitors in the niche actually use,
  - the highest-search-volume category / format / audience phrases,
  - currently trending / "rising" search queries (Google-Trends-style),
  - the long-tail questions the target audience types into search.

Research is done with Perplexity (web-grounded), then structured with one
Claude call. The result is stored in the `channel_keyword_research` table
and refreshed on a schedule (see `is_stale`) or on demand from the
dashboard. Consumed by hypogeum.agents.canonical_metadata_agent and
hypogeum.agents.shorts_metadata via `keyword_context_for_metadata`.

Every function is best-effort and never raises into the caller — a channel
with no research row simply falls back to the channel-brief-derived terms
the metadata agents already produce.

Public API:
    get_keyword_research(channel_id, *, db_path=None) -> dict | None
    is_stale(row, *, max_age_days=14, now=None) -> bool
    refresh_keyword_research(channel_id, *, config=None, db_path=None,
        force=False, now=None) -> dict
    keyword_context_for_metadata(channel_id, *, db_path=None) -> dict | None
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

_AGENT = "keyword_research"
_DEFAULT_MAX_AGE_DAYS = 14
_MODEL_VERSION = "v1"

log = get_logger(agent=_AGENT)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_keyword_research(channel_id: str, *, db_path: Optional[Path] = None) -> Optional[dict]:
    """The cached research row for `channel_id`, or None. Never raises."""
    if not channel_id:
        return None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            row = conn.execute(
                "SELECT channel_id, research_json, researched_at, "
                "source_query_count, total_cost_usd "
                "FROM channel_keyword_research WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        research = json.loads(row["research_json"]) if row["research_json"] else {}
        return {
            "channel_id": row["channel_id"],
            "researched_at": row["researched_at"],
            "source_query_count": row["source_query_count"],
            "total_cost_usd": row["total_cost_usd"],
            **research,
        }
    except Exception as exc:  # noqa: BLE001
        log.warn("keyword_research_read_failed", channel_id=channel_id, error=str(exc))
        return None


def is_stale(row: Optional[dict], *, max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
             now: Optional[datetime] = None) -> bool:
    """True when there is no row, or it is older than `max_age_days`."""
    if not row or not row.get("researched_at"):
        return True
    try:
        ts = str(row["researched_at"]).replace("Z", "+00:00")
        researched = datetime.fromisoformat(ts)
        if researched.tzinfo is None:
            researched = researched.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - researched) > timedelta(days=max_age_days)


def keyword_context_for_metadata(channel_id: str, *,
                                 db_path: Optional[Path] = None) -> Optional[dict]:
    """The trimmed research dict the metadata agents drop into their model
    input. None when no usable research exists yet."""
    row = get_keyword_research(channel_id, db_path=db_path)
    if not row:
        return None
    ctx = {
        "competitor_hashtags": row.get("competitor_hashtags") or [],
        "high_volume_search_terms": row.get("high_volume_search_terms") or [],
        "trending_terms": row.get("trending_terms") or [],
        "audience_questions": (row.get("audience_questions") or [])[:8],
        "researched_at": row.get("researched_at"),
    }
    if not any(ctx[k] for k in ("competitor_hashtags", "high_volume_search_terms", "trending_terms")):
        return None
    return ctx


def metadata_channel_context(channel_id: Optional[str], *,
                             db_path: Optional[Path] = None) -> Optional[dict]:
    """The full "channel_context" payload the metadata agents send to the
    model: the channel's broad genre/format/audience (so a stranger-search
    strategy is possible at all) plus the researched competitor hashtags,
    high-volume search terms and trending queries when they exist.

    Returns None only when there is no channel at all. Never raises.
    """
    if not channel_id:
        return None
    try:
        from hypogeum.channel_creative_brief import channel_brief_for
        brief = channel_brief_for(channel_id) or {}
    except Exception:  # noqa: BLE001
        brief = {}
    ctx = {
        "genre": brief.get("genre") or "",
        "format": brief.get("format") or "",
        "audience": brief.get("audience") or "",
        "brand_is_unknown": True,
    }
    research = keyword_context_for_metadata(channel_id, db_path=db_path)
    if research:
        ctx["competitor_hashtags"] = research["competitor_hashtags"]
        ctx["high_volume_search_terms"] = research["high_volume_search_terms"]
        ctx["trending_terms"] = research["trending_terms"]
        ctx["audience_questions"] = research["audience_questions"]
        ctx["research_updated"] = research.get("researched_at")
    if not any(ctx.get(k) for k in ("genre", "format", "audience")):
        return None
    return ctx


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

_STRUCT_SYSTEM_PROMPT = """You are a video-SEO analyst. You will be given three
raw web-research answers about a video channel's niche: (1) successful
competitors and the hashtags/keywords they use, (2) high-volume and
trending/rising search terms for the category, (3) the long-tail questions
the target audience searches.

Distil them into ONE JSON object of clean, reusable discovery terms for this
channel. Rules:
  - "high_volume_search_terms": 12-20 lowercase phrases people actually type
    to find this KIND of content (category + format + audience), ordered
    BROADEST / highest-volume first. Include natural plurals and close
    variants of the core phrase. NO proper nouns, NO channel names, NO
    episode specifics, NO hashtags.
  - "competitor_hashtags": 10-20 hashtags (each starting with #, no spaces)
    that competitors in this niche consistently use. Broad category
    hashtags first.
  - "trending_terms": 5-12 currently rising / trending / breakout phrases
    for this category (from the trends answer). Lowercase, no hashtags.
  - "audience_questions": 5-10 exact long-tail search questions the audience
    types (from answer 3), lowercase, no trailing punctuation.
  - "notable_competitors": up to 8 channel/account names mentioned as
    successful in this niche.
Every list must be de-duplicated and contain only real, usable strings.

Return ONLY the JSON object:
{"high_volume_search_terms": [...], "competitor_hashtags": ["#..."],
 "trending_terms": [...], "audience_questions": [...],
 "notable_competitors": [...]}"""


def refresh_keyword_research(
    channel_id: str,
    *,
    config=None,
    db_path: Optional[Path] = None,
    force: bool = False,
    now: Optional[datetime] = None,
    perplexity_query=None,
    claude_call=None,
) -> dict:
    """
    Research and cache the channel's discovery landscape.

    Skips the work (returns the existing row) when a fresh row already
    exists and `force` is False. Never raises — on any failure it logs and
    returns whatever row currently exists (possibly None-shaped as
    {"channel_id": ..., "error": ...}).

    `perplexity_query` / `claude_call` are injectable for tests; the real
    calls go through hypogeum.perplexity.query_perplexity and
    hypogeum.openrouter.call_claude.
    """
    existing = get_keyword_research(channel_id, db_path=db_path)
    if existing and not force and not is_stale(existing, now=now):
        return existing

    if config is None:
        try:
            from hypogeum.config import Config
            config = Config()
        except Exception as exc:  # noqa: BLE001
            log.error("keyword_research_no_config", channel_id=channel_id, error=str(exc))
            return existing or {"channel_id": channel_id, "error": "no config"}

    # ── channel context ──────────────────────────────────────────────────
    try:
        from hypogeum.channel_creative_brief import build_creative_brief
        brief = build_creative_brief(channel_id, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        log.error("keyword_research_no_brief", channel_id=channel_id, error=str(exc))
        return existing or {"channel_id": channel_id, "error": "no channel brief"}

    genre = (brief.get("genre") or "").strip() or "short video content"
    fmt = (brief.get("format") or "short video").strip()
    audience = (brief.get("audience") or "a general audience").strip()
    year = (now or datetime.now(timezone.utc)).year

    queries = [
        (f"Identify the most successful {genre} channels and accounts on YouTube, "
         f"TikTok and Instagram aimed at {audience}. For each, list the exact "
         f"hashtags they use most often and the recurring keywords and phrases in "
         f"their video titles and descriptions. Focus on the terms that drive "
         f"their discovery, not their branding."),
        (f"What are the highest search-volume and currently trending keywords, "
         f"hashtags and search phrases for {genre} ({fmt}) aimed at {audience} in "
         f"{year}? Include Google Trends rising queries and breakout terms. Give "
         f"the raw phrases people actually type into search."),
        (f"What exact phrases and questions do {audience} type into YouTube and "
         f"Google search when looking for {genre} content? List the long-tail "
         f"search queries, most common first."),
    ]

    pq = perplexity_query
    if pq is None:
        from hypogeum.perplexity import query_perplexity as _qp

        def pq(q):  # noqa: ANN001
            return _qp(q, config, agent=_AGENT, channel_id=channel_id, phase_id="keyword_research")

    raw_answers: list[str] = []
    cost = 0.0
    for i, q in enumerate(queries, 1):
        try:
            res = pq(q)
            raw_answers.append(getattr(res, "content", "") or str(res))
            usage = getattr(res, "usage", {}) or {}
            cost += (usage.get("total_tokens", 0) or 0) * 3e-6
        except Exception as exc:  # noqa: BLE001
            log.warn("keyword_research_query_failed", channel_id=channel_id,
                     query_index=i, error=str(exc))
    if not raw_answers:
        log.error("keyword_research_all_queries_failed", channel_id=channel_id)
        return existing or {"channel_id": channel_id, "error": "research queries failed"}

    # ── structure with Claude ────────────────────────────────────────────
    cc = claude_call
    if cc is None:
        from hypogeum.openrouter import call_claude as _cc

        def cc(system_prompt, payload):  # noqa: ANN001
            r = _cc(
                model_slug=getattr(config, "SHORTS_METADATA_MODEL_SLUG",
                                   "anthropic/claude-sonnet-4.6"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                max_tokens=1500, config=config, agent=_AGENT, channel_id=channel_id,
            )
            txt = r.content.strip()
            if txt.startswith("```"):
                txt = txt.strip("`")
                if txt.lower().startswith("json"):
                    txt = txt[4:]
            return json.loads(txt)

    try:
        structured = cc(_STRUCT_SYSTEM_PROMPT, {
            "channel": {"genre": genre, "format": fmt, "audience": audience},
            "competitor_research": raw_answers[0] if len(raw_answers) > 0 else "",
            "trends_research": raw_answers[1] if len(raw_answers) > 1 else "",
            "audience_intent_research": raw_answers[2] if len(raw_answers) > 2 else "",
        })
    except Exception as exc:  # noqa: BLE001
        log.error("keyword_research_structuring_failed", channel_id=channel_id, error=str(exc))
        return existing or {"channel_id": channel_id, "error": "structuring failed"}

    def _clean_list(v, *, hashtag=False, limit=25):
        out, seen = [], set()
        for item in (v or []):
            if not isinstance(item, str):
                continue
            s = item.strip()
            if hashtag:
                s = "#" + s.lstrip("#").replace(" ", "")
                if len(s) < 3:
                    continue
            else:
                s = s.lower().strip().rstrip("?.!,")
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            out.append(s)
            if len(out) >= limit:
                break
        return out

    research = {
        "high_volume_search_terms": _clean_list(structured.get("high_volume_search_terms"), limit=22),
        "competitor_hashtags": _clean_list(structured.get("competitor_hashtags"), hashtag=True, limit=22),
        "trending_terms": _clean_list(structured.get("trending_terms"), limit=14),
        "audience_questions": _clean_list(structured.get("audience_questions"), limit=12),
        "notable_competitors": [c.strip() for c in (structured.get("notable_competitors") or [])
                                if isinstance(c, str) and c.strip()][:8],
        "genre": genre,
        "audience": audience,
    }

    researched_at = (now or datetime.now(timezone.utc)).isoformat()
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            conn.execute(
                "INSERT INTO channel_keyword_research "
                "(channel_id, research_json, researched_at, source_query_count, "
                " total_cost_usd, metadata_version) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "  research_json = excluded.research_json, "
                "  researched_at = excluded.researched_at, "
                "  source_query_count = excluded.source_query_count, "
                "  total_cost_usd = excluded.total_cost_usd, "
                "  metadata_version = excluded.metadata_version",
                (channel_id, json.dumps(research), researched_at,
                 len(raw_answers), round(cost, 6), _MODEL_VERSION),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.error("keyword_research_write_failed", channel_id=channel_id, error=str(exc))
        return {"channel_id": channel_id, "researched_at": researched_at, **research,
                "error": "not persisted"}

    log.info("keyword_research_refreshed", channel_id=channel_id,
             query_count=len(raw_answers), terms=len(research["high_volume_search_terms"]),
             hashtags=len(research["competitor_hashtags"]), cost_usd=round(cost, 4))
    return {"channel_id": channel_id, "researched_at": researched_at,
            "source_query_count": len(raw_answers), "total_cost_usd": round(cost, 6),
            **research}
