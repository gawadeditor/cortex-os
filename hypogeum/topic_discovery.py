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

# Scoring weights for composite score (retained for reference)
_W_CTR        = 0.35
_W_RETENTION  = 0.35
_W_WATCH_TIME = 0.20
_W_NOVELTY    = 0.10

# Signal scoring deltas applied on top of the heuristic base score.
# Keep as named constants so they can be tuned without hunting inline literals.
_SIGNAL_BOOST_TOP_PERFORMER = 0.12   # topic words overlap a top-performer insight
_SIGNAL_BOOST_HIGH_RETENTION = 0.08  # topic words overlap a high-retention insight
_SIGNAL_PENALTY_LOW_RETENTION = 0.10 # topic words overlap a low-retention / hook-failure insight
_SIGNAL_NOVELTY_PENALTY = 0.70       # multiplier for recently queued topics (unchanged)

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


def run_discovery_cycle(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Run one discovery cycle for channel_id.

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
            "SELECT channel_id, topic, selected, status FROM topic_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "candidate not found", "code": "not_found"}
        channel_id, topic_text, selected, status = row
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

        # Enqueue via topics module
        from hypogeum.topics import enqueue_topic, DuplicateTopicError
        try:
            topic_id = enqueue_topic(
                channel_id, topic_text,
                priority=10,
                validate_channel=True,
                db_path=db_path,
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
