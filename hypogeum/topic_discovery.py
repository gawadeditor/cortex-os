"""
hypogeum/topic_discovery.py — Autonomous topic discovery for the Factory OS.

Produces scored topic candidates from existing analytics signals and learning
records. No LLM calls, no generation, no pipeline invocations — pure DB reads
and heuristic scoring.

Discovery cycle lifecycle:
    run_discovery_cycle() reads platform_analytics + analytics_insights +
    learning_records, generates 5–10 candidate rows in topic_candidates,
    auto-selects the highest-scoring candidate (selected=1, status stays
    'pending' until operator approves).

Public API:
    run_discovery_cycle(channel_id, db_path=None) -> dict
    get_candidates(channel_id, cycle_id=None, limit=20, db_path=None) -> list[dict]
    get_latest_cycle_id(channel_id, db_path=None) -> str | None
    approve_candidate(candidate_id, db_path=None) -> dict
    reject_candidate(candidate_id, reason=None, db_path=None) -> dict

Signal scoring (M5.1):
    _build_signal_context() reads both platform_analytics and analytics_insights
    to compute per-channel KPI baselines and keyword sets that _score_seed()
    uses to boost or penalise topic candidates relative to channel performance.
    When no real analytics exist the function returns has_real_analytics=False
    and _score_seed() falls back to static heuristic scores — identical to
    pre-M5.1 behaviour.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

# ---------------------------------------------------------------------------
# Heuristic topic seeds — expanded from channel signals + hardcoded fallbacks.
# In a future milestone these are replaced by LLM-driven ideation.
# ---------------------------------------------------------------------------

_FALLBACK_SEEDS: list[dict] = [
    {
        "topic": "The hidden history of ancient underground cities",
        "source": "heuristic",
        "ctr_rationale": "Mystery/history titles average +18% CTR on archival footage channels",
        "retention_rationale": "Underground/architecture subject matter retains 65–70% avg watch %",
        "watch_time_rationale": "12–15 min runtime sweet spot for this topic category",
        "risk": "Competitive — many similar videos; differentiate with cinematic opening",
    },
    {
        "topic": "Lost civilisations: what archaeology refuses to explain",
        "source": "heuristic",
        "ctr_rationale": "Controversy framing drives 2–3× click-through vs neutral titles",
        "retention_rationale": "Multi-part narrative structure boosts completion rate",
        "watch_time_rationale": "Audience expects 10–20 min deep dives; pacing is critical",
        "risk": "Risk of sensationalism — keep claims evidence-based",
    },
    {
        "topic": "The architecture of power: buildings that shaped empires",
        "source": "heuristic",
        "ctr_rationale": "Visual-first thumbnail opportunity (colosseum, pyramids, etc.)",
        "retention_rationale": "Architecture + power dynamics = strong retention pattern",
        "watch_time_rationale": "Could support 15–20 min with good visual variety",
        "risk": "Low — well-covered topic; differentiation through production quality",
    },
    {
        "topic": "Ancient trade routes that built the modern world",
        "source": "heuristic",
        "ctr_rationale": "Curiosity gap title; maps and trade networks drive high engagement",
        "retention_rationale": "Geo-narrative structure maintains momentum across segments",
        "watch_time_rationale": "12–18 min with map animations and voiceover",
        "risk": "Medium — geography content can lose viewers if pacing slows",
    },
    {
        "topic": "The real story behind the world's most mysterious ruins",
        "source": "heuristic",
        "ctr_rationale": "'Real story' framing reliably outperforms generic titles",
        "retention_rationale": "Ruins + mystery = high curiosity-driven retention",
        "watch_time_rationale": "8–12 min; tight pacing essential",
        "risk": "Low — evergreen topic, strong visual asset availability",
    },
    {
        "topic": "How ancient engineers solved problems we still can't explain",
        "source": "heuristic",
        "ctr_rationale": "Engineering mystery angle attracts crossover STEM audience",
        "retention_rationale": "Problem → solution structure maintains engagement arc",
        "watch_time_rationale": "10–15 min; visual demonstrations help retention",
        "risk": "Medium — requires accurate engineering details; fact-check critical",
    },
    {
        "topic": "Secret societies and the empires they shaped",
        "source": "heuristic",
        "ctr_rationale": "Secret society content is perennially high-CTR",
        "retention_rationale": "Conspiracy-adjacent but scholarly framing = strong completion",
        "watch_time_rationale": "15–20 min; episodic narrative structure works well",
        "risk": "High — misinformation risk; stay within documented historical record",
    },
]

# Signal scoring deltas for _score_seed() — v1 heuristic path only.
# These constants belong to the v1 legacy scorer. Do not tune here;
# use initial_scorer.py (v2) for the configurable structural scorer.
_SIGNAL_BOOST_TOP_PERFORMER  = 0.12
_SIGNAL_BOOST_HIGH_RETENTION = 0.08
_SIGNAL_PENALTY_LOW_RETENTION = 0.10
_SIGNAL_NOVELTY_PENALTY      = 0.70

# Stopwords excluded from keyword tokenisation to avoid spurious overlap.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "but",
    "that", "this", "with", "for", "by", "from", "as", "is", "was", "are",
    "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may", "might", "what",
    "how", "why", "when", "where", "which", "who", "we", "they", "their",
    "our", "its", "it", "he", "she", "you", "your", "my",
})


def _tokenise(text: str) -> set[str]:
    """Return lowercase word set, excluding stopwords and short tokens."""
    import re
    words = re.findall(r"[a-z']+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _build_signal_context(channel_id: str, conn) -> dict:
    """
    Build a per-channel signal context from platform_analytics and analytics_insights.

    Reads both tables so that scoring reflects real performance data when available.
    When no real analytics exist (source='mock' or no rows), returns a context with
    has_real_analytics=False — _score_seed() must treat this as a no-op and produce
    the same scores as the pre-M5.1 heuristic.

    Returns:
        {
          "has_real_analytics": bool,
          "top_performer_keywords": set[str],   # boost candidates
          "high_retention_keywords": set[str],  # secondary boost
          "penalty_keywords": set[str],          # penalise candidates
          "avg_ctr": float | None,
          "avg_view_pct": float | None,
          "avg_watch_time_s": float | None,
        }
    """
    empty: dict = {
        "has_real_analytics":    False,
        "top_performer_keywords": set(),
        "high_retention_keywords": set(),
        "penalty_keywords":       set(),
        "avg_ctr":                None,
        "avg_view_pct":           None,
        "avg_watch_time_s":       None,
    }

    try:
        # ── 1. Channel KPI baselines from platform_analytics (real data only) ──
        # Episodes are linked to channels via the channel_episodes join table
        # (managed by channel_manager._ensure_tables).  If that table does not
        # exist yet (bare DB), the except block returns the empty context.
        kpi_row = conn.execute(
            """SELECT AVG(ctr), AVG(avg_view_pct), AVG(avg_watch_time_s)
               FROM platform_analytics
               WHERE source != 'mock'
                 AND episode_id IN (
                     SELECT episode_id FROM channel_episodes WHERE channel_id = ?
                 )""",
            (channel_id,),
        ).fetchone()

        avg_ctr          = kpi_row[0] if kpi_row else None
        avg_view_pct     = kpi_row[1] if kpi_row else None
        avg_watch_time_s = kpi_row[2] if kpi_row else None

        has_real_kpis = any(v is not None for v in (avg_ctr, avg_view_pct, avg_watch_time_s))

        # ── 2. Keyword sets from analytics_insights ──────────────────────────
        insight_rows = conn.execute(
            """SELECT ai.insight_type, ai.detail
               FROM analytics_insights ai
               WHERE ai.episode_id IN (
                   SELECT episode_id FROM channel_episodes WHERE channel_id = ?
               )
               ORDER BY ai.created_at DESC LIMIT 40""",
            (channel_id,),
        ).fetchall()

        top_performer_keywords: set[str] = set()
        high_retention_keywords: set[str] = set()
        penalty_keywords: set[str] = set()

        for insight_type, detail in insight_rows:
            if not detail:
                continue
            words = _tokenise(detail)
            if insight_type in ("top_performer", "trend", "audience_segment"):
                top_performer_keywords |= words
            elif insight_type in ("high_retention",):
                high_retention_keywords |= words
            elif insight_type in ("hook_failure", "low_retention", "weak_thumbnail"):
                penalty_keywords |= words

        has_insight_data = bool(top_performer_keywords or high_retention_keywords or penalty_keywords)

        if not has_real_kpis and not has_insight_data:
            return empty

        return {
            "has_real_analytics":     True,
            "top_performer_keywords": top_performer_keywords,
            "high_retention_keywords": high_retention_keywords,
            "penalty_keywords":        penalty_keywords,
            "avg_ctr":                avg_ctr,
            "avg_view_pct":           avg_view_pct,
            "avg_watch_time_s":       avg_watch_time_s,
        }

    except Exception:
        # Never crash discovery due to analytics errors — degrade gracefully.
        return empty


def _score_seed(seed: dict, existing_topics: set[str], signal_ctx: dict | None = None) -> float:
    """
    Assign a composite score 0.0–1.0 to a seed topic.

    When signal_ctx is None or has_real_analytics=False, scoring is identical
    to pre-M5.1 behaviour (hardcoded heuristic base scores + novelty penalty).

    When signal_ctx contains real analytics:
    - Topics whose words overlap top-performer insight keywords receive a boost.
    - Topics whose words overlap high-retention insight keywords receive a
      smaller secondary boost.
    - Topics whose words overlap low-retention / hook-failure keywords are
      penalised.
    - The existing novelty penalty is applied last, after signal adjustments.
    - Score is clamped to [0.0, 1.0].
    """
    base_scores = {
        "The hidden history of ancient underground cities": 0.82,
        "Lost civilisations: what archaeology refuses to explain": 0.78,
        "The architecture of power: buildings that shaped empires": 0.71,
        "Ancient trade routes that built the modern world": 0.67,
        "The real story behind the world's most mysterious ruins": 0.74,
        "How ancient engineers solved problems we still can't explain": 0.69,
        "Secret societies and the empires they shaped": 0.64,
    }
    score = base_scores.get(seed["topic"], 0.60)

    # Signal adjustments — only when real analytics data is available.
    if signal_ctx and signal_ctx.get("has_real_analytics"):
        topic_words = _tokenise(seed["topic"])
        if topic_words & signal_ctx["top_performer_keywords"]:
            score += _SIGNAL_BOOST_TOP_PERFORMER
        if topic_words & signal_ctx["high_retention_keywords"]:
            score += _SIGNAL_BOOST_HIGH_RETENTION
        if topic_words & signal_ctx["penalty_keywords"]:
            score -= _SIGNAL_PENALTY_LOW_RETENTION

    # Novelty penalty: reduce if this topic was recently queued/produced.
    norm = seed["topic"].lower().strip()
    if any(norm in t.lower() for t in existing_topics):
        score *= _SIGNAL_NOVELTY_PENALTY

    return round(max(0.0, min(1.0, score)), 4)


def _get_existing_topics(channel_id: str, conn) -> set[str]:
    """Return recently used topic texts for this channel (consumed + queued)."""
    rows = conn.execute(
        """SELECT topic_text FROM episode_topics
           WHERE channel_id = ? AND status IN ('queued','claimed','consumed')
           ORDER BY created_at DESC LIMIT 50""",
        (channel_id,),
    ).fetchall()
    return {r[0] for r in rows}


def _get_signal_hints(channel_id: str, conn) -> list[str]:
    """Extract topic hints from recent analytics insights for this channel.

    Superseded by _build_signal_context() in M5.1 but retained for any
    external callers that may depend on the function signature.
    """
    rows = conn.execute(
        """SELECT ai.detail FROM analytics_insights ai
           JOIN episodes e ON ai.episode_id = e.episode_id
           WHERE e.channel_id = ?
             AND ai.insight_type IN ('top_performer','audience_segment','trend')
           ORDER BY ai.created_at DESC LIMIT 10""",
        (channel_id,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")
_PUNCT_RE   = re.compile(r"[^a-z0-9\s]")
_WS_RE      = re.compile(r"\s+")


def normalize_topic_text(topic_text: str) -> str:
    """
    Full normalization pipeline applied before canonical slug generation.

    Sequence (order is significant):
      1. trim leading/trailing whitespace
      2. lowercase
      3. strip leading English article (the / a / an)
      4. remove punctuation (anything that is not a-z, 0-9, or whitespace)
      5. collapse consecutive whitespace to a single space
      6. trim again

    After this function, all five variants from the design doc converge:
        "Lost City of Helike"     → "lost city of helike"
        "The Lost City of Helike" → "lost city of helike"
        "Lost City Of Helike"     → "lost city of helike"
        "Lost City of Helike?"    → "lost city of helike"
        "Lost City of Helike!"    → "lost city of helike"

    Returns a normalized, lowercase, punctuation-free string with single
    spaces. NOT yet slugified (no underscores).
    """
    text = topic_text.strip().lower()
    text = _ARTICLE_RE.sub("", text)
    text = _PUNCT_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _make_canonical_subject(topic_text: str) -> str:
    """
    Deterministic canonical subject key derived from topic_text only.

    Angle is intentionally excluded: multiple angles on the same underlying
    subject must resolve to one shared canonical identity. Used for:
    source-verification caching, opportunity deduplication, novelty scoring,
    discovery memory, and topic-graph node identity.

    Delegates normalization to normalize_topic_text(), then converts spaces
    to underscores and truncates at 200 characters.

    Stable contract: same topic_text (after normalization) always produces
    the same output, regardless of angle or any other candidate field.
    """
    slug = _WS_RE.sub("_", normalize_topic_text(topic_text))
    return slug[:200]


def _build_v2_config():
    """
    Build a minimal config SimpleNamespace from os.environ for the v2 director.

    By the time _run_discovery_cycle_v2_stub() is called from the worker,
    the worker has already instantiated Config (which calls _load_dotenv and
    populates os.environ).  Reading env vars here picks up .env values without
    requiring a second Config instantiation (which would need OPENROUTER_API_KEY).
    """
    from types import SimpleNamespace

    def _bool(key: str, default: bool = False) -> bool:
        raw = os.getenv(key, "true" if default else "false").strip().lower()
        return raw in ("true", "1", "yes")

    def _float(key: str, default: float) -> float:
        try:
            return float(os.getenv(key, str(default)) or str(default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, str(default)) or str(default))
        except (TypeError, ValueError):
            return default

    return SimpleNamespace(
        AUTO_DISCOVERY_AUTO_APPROVE = _bool("AUTO_DISCOVERY_AUTO_APPROVE"),
        DISCOVERY_VERIFY_THRESHOLD  = _float("DISCOVERY_VERIFY_THRESHOLD", 0.65),
        DISCOVERY_VERIFY_DELTA      = _float("DISCOVERY_VERIFY_DELTA", 0.10),
        # OpenRouter — required by openrouter._build_headers()
        OPENROUTER_API_KEY         = os.getenv("OPENROUTER_API_KEY", ""),
        OPENROUTER_SONNET_MODEL_ID = os.getenv(
            "OPENROUTER_SONNET_MODEL_ID", "anthropic/claude-sonnet-4.6"
        ),
        OPENROUTER_APP_NAME        = os.getenv("OPENROUTER_APP_NAME", "hypogeum-brain"),
        OPENROUTER_APP_URL         = os.getenv("OPENROUTER_APP_URL"),
        # Perplexity
        PERPLEXITY_API_KEY    = os.getenv("PERPLEXITY_API_KEY"),
        PERPLEXITY_MODEL      = os.getenv("PERPLEXITY_MODEL", "sonar-pro"),
        PERPLEXITY_MAX_TOKENS = _int("PERPLEXITY_MAX_TOKENS", 2048),
    )


def _run_discovery_cycle_v2_stub(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """Dispatch to Discovery Director v2 when DISCOVERY_ENGINE=v2."""
    from hypogeum.agents.discovery_director import run as _director_run
    cfg = _build_v2_config()
    result = _director_run(channel_id, config=cfg, db_path=db_path)
    # Patch in v1-compatible keys so existing consumers don't break on a
    # partial dict (run_worker reads selected_candidate_id and candidates).
    result.setdefault("selected_candidate_id", None)
    result.setdefault("candidates", result.get("opportunities", []))
    return result


# ---------------------------------------------------------------------------
# discovery_state helpers — engine-agnostic persistent run state
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_candidate_v2(opp: dict) -> dict:
    """Convert a v2 opportunity dict to the template-compatible candidate shape."""
    status_map = {
        "active":      "pending",
        "selected":    "pending",
        "enqueued":    "approved",
        "consumed":    "consumed",
        "rejected":    "rejected",
        "blacklisted": "rejected",
        "exhausted":   "rejected",
    }
    opp_status = opp.get("status", "active")
    return {
        "candidate_id":         opp.get("opportunity_id", str(uuid.uuid4())),
        "topic":                opp.get("topic_text") or opp.get("canonical_subject", ""),
        "score":                round(float(
            opp.get("final_composite")
            or opp.get("priority_score")
            or opp.get("initial_composite")
            or 0.0
        ), 4),
        "source":               opp.get("generation_source", "llm"),
        "status":               status_map.get(opp_status, "pending"),
        "selected":             1 if opp_status in ("selected", "enqueued") else 0,
        "ctr_rationale":        opp.get("mystery_hook", ""),
        "retention_rationale":  opp.get("differentiation_claim", ""),
        "watch_time_rationale": opp.get("visual_signal", ""),
        "risk":                 None,
        "cycle_id":             opp.get("cycle_id"),
    }


def upsert_discovery_state(
    channel_id: str,
    status: str,
    *,
    last_cycle_id: Optional[str] = None,
    candidates: Optional[list] = None,
    error: Optional[str] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Write or update the discovery_state row for a channel.

    Fields not explicitly provided are preserved via COALESCE on conflict.
    candidate_count and candidates_json are only updated when candidates is given.
    """
    now = _now_iso()
    has_candidates = candidates is not None
    count = len(candidates) if has_candidates else 0
    cjson = json.dumps(candidates) if has_candidates else None

    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO discovery_state
               (channel_id, status, last_cycle_id, candidate_count, candidates_json,
                started_at, completed_at, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 status          = excluded.status,
                 last_cycle_id   = COALESCE(excluded.last_cycle_id,   last_cycle_id),
                 candidate_count = CASE WHEN excluded.candidates_json IS NOT NULL
                                   THEN excluded.candidate_count
                                   ELSE candidate_count END,
                 candidates_json = CASE WHEN excluded.candidates_json IS NOT NULL
                                   THEN excluded.candidates_json
                                   ELSE candidates_json END,
                 started_at      = COALESCE(excluded.started_at,      started_at),
                 completed_at    = excluded.completed_at,
                 error           = excluded.error,
                 updated_at      = excluded.updated_at""",
            (channel_id, status, last_cycle_id, count, cjson,
             started_at, completed_at, error, now),
        )
        conn.commit()
    finally:
        conn.close()


_STALE_RUNNING_MINUTES = 15  # after this, a 'running' state is auto-reset to failed


def get_discovery_state(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Return the current discovery_state row for a channel.

    If status='running' but started_at is older than _STALE_RUNNING_MINUTES,
    the row is automatically updated to status='failed' with a timeout error
    so the page never stays stuck in Running state after a server restart or
    thread death.

    Returns a dict with keys:
        status, last_cycle_id, candidate_count, candidates,
        started_at, completed_at, error, updated_at

    Returns an idle default dict if no row exists yet.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT status, last_cycle_id, candidate_count, candidates_json,
                      started_at, completed_at, error, updated_at
               FROM discovery_state WHERE channel_id = ?""",
            (channel_id,),
        ).fetchone()

        if row and row[0] == "running" and row[4]:
            try:
                from datetime import timedelta
                started = datetime.fromisoformat(row[4])
                if (datetime.now(timezone.utc) - started).total_seconds() > _STALE_RUNNING_MINUTES * 60:
                    now = _now_iso()
                    conn.execute(
                        """UPDATE discovery_state
                           SET status='failed', error=?, completed_at=?, updated_at=?
                           WHERE channel_id=?""",
                        (
                            f"Cycle timed out — no response in >{_STALE_RUNNING_MINUTES} minutes. "
                            "Click 'Run Discovery Cycle' to retry.",
                            now, now, channel_id,
                        ),
                    )
                    conn.commit()
                    # Re-fetch updated row
                    row = conn.execute(
                        """SELECT status, last_cycle_id, candidate_count, candidates_json,
                                  started_at, completed_at, error, updated_at
                           FROM discovery_state WHERE channel_id = ?""",
                        (channel_id,),
                    ).fetchone()
            except Exception:
                pass
    finally:
        conn.close()

    if not row:
        return {
            "status":          "idle",
            "last_cycle_id":   None,
            "candidate_count": 0,
            "candidates":      [],
            "started_at":      None,
            "completed_at":    None,
            "error":           None,
            "updated_at":      None,
        }

    candidates = []
    if row[3]:
        try:
            candidates = json.loads(row[3])
        except (json.JSONDecodeError, TypeError):
            candidates = []

    return {
        "status":          row[0],
        "last_cycle_id":   row[1],
        "candidate_count": row[2] or 0,
        "candidates":      candidates,
        "started_at":      row[4],
        "completed_at":    row[5],
        "error":           row[6],
        "updated_at":      row[7],
    }


def run_discovery_cycle(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Stable public entrypoint for topic discovery.

    Dispatches to the v1 heuristic implementation or the v2 Discovery
    Director based on the DISCOVERY_ENGINE environment variable.

    In both cases, updates discovery_state:
      - Sets status='running' at the start.
      - Sets status='completed' with normalized candidates_json on success.
      - Sets status='failed' with error on failure.

    DISCOVERY_ENGINE=v1 (default) — heuristic seeds, no LLM calls.
    DISCOVERY_ENGINE=v2           — Discovery Director (LLM + Perplexity).

    Returns:
        {ok, cycle_id, channel_id, candidate_count, candidates, ...}
    """
    engine = os.getenv("DISCOVERY_ENGINE", "v1").strip().lower()
    started = _now_iso()
    upsert_discovery_state(channel_id, "running", started_at=started, db_path=db_path)
    try:
        if engine == "v2":
            result = _run_discovery_cycle_v2_stub(channel_id, db_path)
        else:
            result = _run_discovery_cycle_v1(channel_id, db_path)

        if result.get("ok"):
            raw_candidates = result.get("candidates", [])
            # Normalize v2 opportunity dicts to the template-compatible shape
            if engine == "v2":
                normalized = [_normalize_candidate_v2(c) for c in raw_candidates]
            else:
                normalized = raw_candidates  # v1 candidates already in correct shape
            upsert_discovery_state(
                channel_id, "completed",
                last_cycle_id=result.get("cycle_id"),
                candidates=normalized,
                started_at=started,
                completed_at=_now_iso(),
                db_path=db_path,
            )
            result["candidates"] = normalized
        else:
            upsert_discovery_state(
                channel_id, "failed",
                error=result.get("error", "Discovery failed"),
                started_at=started,
                completed_at=_now_iso(),
                db_path=db_path,
            )
        return result
    except Exception as exc:
        upsert_discovery_state(
            channel_id, "failed",
            error=str(exc),
            started_at=started,
            completed_at=_now_iso(),
            db_path=db_path,
        )
        raise


def _run_discovery_cycle_v1(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Legacy heuristic discovery cycle (v1).

    Reads existing topics + analytics signals, scores heuristic seeds,
    inserts candidates into topic_candidates, marks the top scorer as
    selected=1.

    Returns:
        {cycle_id, channel_id, candidate_count, selected_candidate_id, candidates}
    """
    cycle_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)

    try:
        existing_topics = _get_existing_topics(channel_id, conn)
        signal_ctx = _build_signal_context(channel_id, conn)

        # Score all seeds using real analytics when available
        scored: list[dict] = []
        for seed in _FALLBACK_SEEDS:
            score = _score_seed(seed, existing_topics, signal_ctx)
            scored.append({
                "candidate_id": str(uuid.uuid4()),
                "channel_id": channel_id,
                "cycle_id": cycle_id,
                "topic": seed["topic"],
                "source": seed["source"],
                "score": score,
                "ctr_rationale": seed["ctr_rationale"],
                "retention_rationale": seed["retention_rationale"],
                "watch_time_rationale": seed["watch_time_rationale"],
                "risk": seed["risk"],
                "selected": 0,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            })

        # Sort descending by score; top item is selected
        scored.sort(key=lambda c: c["score"], reverse=True)
        selected_id = scored[0]["candidate_id"] if scored else None
        if selected_id:
            scored[0]["selected"] = 1

        # Insert all candidates
        conn.executemany(
            """INSERT INTO topic_candidates
               (candidate_id, channel_id, cycle_id, topic, source, score,
                ctr_rationale, retention_rationale, watch_time_rationale,
                risk, selected, status, created_at, updated_at)
               VALUES
               (:candidate_id, :channel_id, :cycle_id, :topic, :source, :score,
                :ctr_rationale, :retention_rationale, :watch_time_rationale,
                :risk, :selected, :status, :created_at, :updated_at)""",
            scored,
        )
        conn.commit()

        return {
            "ok": True,
            "cycle_id": cycle_id,
            "channel_id": channel_id,
            "candidate_count": len(scored),
            "selected_candidate_id": selected_id,
            "candidates": scored,
        }
    finally:
        conn.close()


def get_candidates(
    channel_id: str,
    cycle_id: Optional[str] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return candidates for a channel, newest cycle first, ordered by score desc."""
    conn = get_connection(db_path)
    try:
        if cycle_id:
            rows = conn.execute(
                """SELECT candidate_id, channel_id, cycle_id, topic, source, score,
                          ctr_rationale, retention_rationale, watch_time_rationale,
                          risk, selected, status, created_at, updated_at
                   FROM topic_candidates
                   WHERE channel_id = ? AND cycle_id = ?
                   ORDER BY score DESC LIMIT ?""",
                (channel_id, cycle_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT candidate_id, channel_id, cycle_id, topic, source, score,
                          ctr_rationale, retention_rationale, watch_time_rationale,
                          risk, selected, status, created_at, updated_at
                   FROM topic_candidates
                   WHERE channel_id = ?
                   ORDER BY created_at DESC, score DESC LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        cols = [
            "candidate_id", "channel_id", "cycle_id", "topic", "source", "score",
            "ctr_rationale", "retention_rationale", "watch_time_rationale",
            "risk", "selected", "status", "created_at", "updated_at",
        ]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def get_latest_cycle_id(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Return the cycle_id of the most recent discovery run for this channel."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT cycle_id FROM topic_candidates
               WHERE channel_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (channel_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def approve_candidate(
    candidate_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Approve the selected candidate: enqueue it as an episode_topic.

    Only candidates with selected=1 and status='pending' may be approved.
    Sets status='approved', then calls enqueue_topic().

    Returns:
        {ok, candidate_id, topic_id, topic_text}
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT channel_id, topic, selected, status, score FROM topic_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "candidate not found", "code": "not_found"}
        channel_id, topic_text, selected, status, score = row
        if not selected:
            return {"ok": False, "error": "only the selected candidate may be approved", "code": "not_selected"}
        if status != "pending":
            return {"ok": False, "error": f"candidate status is '{status}', expected 'pending'", "code": "wrong_status"}

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE topic_candidates SET status='approved', updated_at=? WHERE candidate_id=?",
            (now, candidate_id),
        )
        conn.commit()

        # Enqueue via topics module, preserving discovery provenance
        from hypogeum.topics import enqueue_topic, DuplicateTopicError
        try:
            topic_id = enqueue_topic(
                channel_id, topic_text,
                priority=10,
                validate_channel=True,
                db_path=db_path,
                source="discovered",
                discovery_score=float(score) if score is not None else None,
                candidate_id=candidate_id,
            )
        except DuplicateTopicError:
            # Already queued — treat as success
            row2 = conn.execute(
                "SELECT topic_id FROM episode_topics WHERE channel_id=? AND topic_text=? AND status='queued'",
                (channel_id, topic_text),
            ).fetchone()
            topic_id = row2[0] if row2 else "duplicate"

        return {
            "ok": True,
            "candidate_id": candidate_id,
            "topic_id": topic_id,
            "topic_text": topic_text,
            "channel_id": channel_id,
            "discovery_score": float(score) if score is not None else None,
        }
    finally:
        conn.close()


def reject_candidate(
    candidate_id: str,
    reason: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Mark a candidate as rejected."""
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        updated = conn.execute(
            "UPDATE topic_candidates SET status='rejected', risk=COALESCE(?, risk), updated_at=? WHERE candidate_id=?",
            (reason, now, candidate_id),
        ).rowcount
        conn.commit()
        if not updated:
            return {"ok": False, "error": "candidate not found", "code": "not_found"}
        return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}
    finally:
        conn.close()
