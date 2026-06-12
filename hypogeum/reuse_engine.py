"""
hypogeum/reuse_engine.py — Reuse Recommendation Layer

Scores and ranks archived assets for potential reuse in future episodes.
Bridges the Archive Recall Layer (archive_recall.py) with actionable
reuse decisions at episode run time.

Architecture:
    Pure-Python scoring — no ML, no external calls, no LLM.
    score_asset() is stateless and reads no DB; fully unit-testable.
    recommend_reuse() calls archive_recall.recall_assets(), scores results,
    filters below min_score, sorts descending, and returns ReuseCandidate list.
    should_reuse() calls get_top_candidate() and compares to threshold.

Scoring model (five components, weighted sum):
    quality_score    — from quality_verdict (approved=1.0 … rejected=0.0)
    performance_score — normalized performance_signal [0,1]; None → 0.5
    tag_overlap_score — Jaccard similarity between request_tags and asset tags;
                        no request_tags → 1.0 (no requirement, fully compatible)
    style_match_score — fraction of request_style keys that match;
                        no request_style → 1.0 (no requirement, fully compatible)
    provider_match_score — 1.0 if match, 0.5 if no preference, 0.0 if mismatch

Default weights: quality=0.40, performance=0.30, tag_overlap=0.15,
                 style_match=0.10, provider_match=0.05

Recency multiplier: applied after weighted sum.
    max(0.5, 1.0 - days_since_archived / 730)
    Two-year half-life: assets 2 years old score at 0.5× their raw score.
    Assets less than 2 years old are penalised proportionally.

Side effects:
    should_reuse() records each decision in a `reuse_decisions` table.
    The table is created lazily by _ensure_reuse_table() (not in schema.sql).
    All writes are non-blocking: exceptions are swallowed.

Public API:
    score_asset(
        recall_result, request_tags=None, request_style=None,
        request_provider=None, weights=None,
    ) -> float

    recommend_reuse(
        episode_id, asset_type, provider=None,
        semantic_tags=None, visual_style=None,
        min_score=0.5, limit=10, db_path=None,
    ) -> list[ReuseCandidate]

    get_top_candidate(
        episode_id, asset_type, provider=None,
        semantic_tags=None, visual_style=None,
        min_score=0.5, db_path=None,
    ) -> ReuseCandidate | None

    should_reuse(
        episode_id, asset_type, provider=None,
        semantic_tags=None, visual_style=None,
        threshold=0.6, db_path=None,
    ) -> bool
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.archive_recall import RecallResult, recall_assets
from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Default path (mirrors archive_recall / asset_registry pattern)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# ReuseCandidate dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReuseCandidate:
    """
    A scored, ranked candidate asset recommended for reuse.

    score is the final weighted-and-recency-adjusted value in [0.0, 1.0].
    score_components maps each component name to its raw [0.0, 1.0] value
    before weighting (useful for debugging and display).
    recency_multiplier is the post-weighting adjustment applied (0.5–1.0).
    """
    asset_id:           str
    archive_id:         str
    episode_id:         str          # source episode
    asset_type:         str
    provider:           str
    model:              Optional[str]
    quality_verdict:    str
    score:              float
    score_components:   dict         = field(default_factory=dict)
    recency_multiplier: float        = 1.0
    reuse_eligible:     bool         = True
    semantic_tags:      list         = field(default_factory=list)
    visual_style:       dict         = field(default_factory=dict)
    performance_signal: Optional[float] = None
    archived_at:        str          = ""


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

_QUALITY_SCORES: dict[str, float] = {
    "approved":    1.0,
    "validated":   0.75,
    "unvalidated": 0.5,
    "rejected":    0.0,
}

# Default component weights (must sum to 1.0)
_DEFAULT_WEIGHTS: dict[str, float] = {
    "quality":       0.40,
    "performance":   0.30,
    "tag_overlap":   0.15,
    "style_match":   0.10,
    "provider_match": 0.05,
}

# 2-year half-life for recency penalty; minimum 0.5× multiplier
_RECENCY_HALF_LIFE_DAYS: float = 730.0
_RECENCY_FLOOR: float = 0.5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _parse_archived_at(archived_at: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp string. Returns None on any error."""
    try:
        ts = archived_at.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError, TypeError):
        return None


def _recency_multiplier(archived_at: str) -> float:
    """
    Compute a recency multiplier in [_RECENCY_FLOOR, 1.0].

    Newer assets score closer to 1.0; assets older than 2 years are capped
    at _RECENCY_FLOOR. Invalid timestamps return 1.0 (no penalty).
    """
    dt = _parse_archived_at(archived_at)
    if dt is None:
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return max(_RECENCY_FLOOR, 1.0 - days / _RECENCY_HALF_LIFE_DAYS)


def _jaccard(a: list, b: list) -> float:
    """Jaccard similarity between two tag lists. Both empty → 0.0."""
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def _style_match(request_style: dict, asset_style: dict) -> float:
    """
    Fraction of request_style keys whose value matches asset_style.

    request_style empty → 1.0 (no style requirement = perfect match).
    request_style non-empty but asset_style empty → 0.0.
    """
    if not request_style:
        return 1.0
    if not asset_style:
        return 0.0
    matches = sum(1 for k, v in request_style.items() if asset_style.get(k) == v)
    return matches / len(request_style)


# ---------------------------------------------------------------------------
# reuse_decisions table (created lazily — not in schema.sql)
# ---------------------------------------------------------------------------

_CREATE_REUSE_DECISIONS = """
CREATE TABLE IF NOT EXISTS reuse_decisions (
    decision_id          TEXT NOT NULL PRIMARY KEY,
    episode_id           TEXT NOT NULL,
    asset_type           TEXT NOT NULL,
    provider             TEXT,
    recommended_asset_id TEXT,
    archive_id           TEXT,
    score                REAL,
    decision             TEXT NOT NULL,
    score_components_json TEXT,
    decided_at           TEXT NOT NULL
)
"""


def _ensure_reuse_table(conn) -> None:
    """Create reuse_decisions table if absent. Idempotent."""
    conn.execute(_CREATE_REUSE_DECISIONS)


def _record_decision(
    db_path: Optional[Path],
    episode_id: str,
    asset_type: str,
    provider: Optional[str],
    candidate: Optional[ReuseCandidate],
    decision: str,                # "reuse" | "generate"
) -> None:
    """
    Write one reuse decision row. Non-blocking: exceptions silently ignored.
    """
    import json as _json
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_reuse_table(conn)
            conn.execute(
                """
                INSERT INTO reuse_decisions
                    (decision_id, episode_id, asset_type, provider,
                     recommended_asset_id, archive_id, score, decision,
                     score_components_json, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    episode_id,
                    asset_type,
                    provider,
                    candidate.asset_id  if candidate else None,
                    candidate.archive_id if candidate else None,
                    candidate.score      if candidate else None,
                    decision,
                    _json.dumps(candidate.score_components) if candidate else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking


# ---------------------------------------------------------------------------
# Public API — score_asset
# ---------------------------------------------------------------------------

def score_asset(
    recall_result: RecallResult,
    request_tags:     Optional[list]  = None,
    request_style:    Optional[dict]  = None,
    request_provider: Optional[str]   = None,
    weights:          Optional[dict]  = None,
) -> float:
    """
    Compute a reuse score for one archived asset against a generation request.

    The score is a weighted sum of five components, then multiplied by a
    recency factor. All component values are in [0.0, 1.0]; the final score
    is also clamped to [0.0, 1.0].

    This function is pure (no DB access, no side effects). Suitable for
    unit testing in isolation.

    Args:
        recall_result:    RecallResult from archive_recall.
        request_tags:     Tags the new generation would have (OR semantics).
                          None or [] → tag requirement not specified (score=1.0).
        request_style:    Visual style dict the new generation would have.
                          None or {} → style requirement not specified (score=1.0).
        request_provider: Preferred provider slug. None → no preference.
        weights:          Override default component weights. Must be a dict
                          with the same keys as _DEFAULT_WEIGHTS.

    Returns:
        Reuse score in [0.0, 1.0]. Higher = better candidate for reuse.
    """
    w = weights if weights is not None else _DEFAULT_WEIGHTS

    # 1. Quality
    quality_score = _QUALITY_SCORES.get(recall_result.quality_verdict, 0.5)

    # 2. Performance (clamp to [0,1]; None → 0.5)
    raw_perf = recall_result.performance_signal
    if raw_perf is None:
        performance_score = 0.5
    else:
        performance_score = max(0.0, min(1.0, float(raw_perf)))

    # 3. Tag overlap (Jaccard; no request_tags → 1.0)
    if not request_tags:
        tag_overlap_score = 1.0
    else:
        tag_overlap_score = _jaccard(request_tags, recall_result.semantic_tags)

    # 4. Style match (fraction; no request_style → 1.0)
    style_match_score = _style_match(request_style or {}, recall_result.visual_style)

    # 5. Provider match
    if request_provider is None:
        provider_match_score = 1.0
    elif request_provider.lower() == recall_result.provider.lower():
        provider_match_score = 1.0
    else:
        provider_match_score = 0.0

    # Weighted sum
    raw_score = (
        quality_score      * w.get("quality",       _DEFAULT_WEIGHTS["quality"])
        + performance_score  * w.get("performance",   _DEFAULT_WEIGHTS["performance"])
        + tag_overlap_score  * w.get("tag_overlap",   _DEFAULT_WEIGHTS["tag_overlap"])
        + style_match_score  * w.get("style_match",   _DEFAULT_WEIGHTS["style_match"])
        + provider_match_score * w.get("provider_match", _DEFAULT_WEIGHTS["provider_match"])
    )

    # Recency multiplier
    recency = _recency_multiplier(recall_result.archived_at)
    final_score = max(0.0, min(1.0, raw_score * recency))

    return final_score


def _score_to_candidate(
    r: RecallResult,
    request_tags:     Optional[list],
    request_style:    Optional[dict],
    request_provider: Optional[str],
    weights:          Optional[dict],
) -> ReuseCandidate:
    """Build a ReuseCandidate from a RecallResult with all score metadata."""
    w = weights or _DEFAULT_WEIGHTS

    quality_score      = _QUALITY_SCORES.get(r.quality_verdict, 0.5)
    raw_perf           = r.performance_signal
    performance_score  = 0.5 if raw_perf is None else max(0.0, min(1.0, float(raw_perf)))
    tag_overlap_score  = 1.0 if not request_tags else _jaccard(request_tags, r.semantic_tags)
    style_match_score  = _style_match(request_style or {}, r.visual_style)

    if request_provider is None:
        provider_match_score = 1.0
    elif request_provider.lower() == r.provider.lower():
        provider_match_score = 1.0
    else:
        provider_match_score = 0.0

    raw_score = (
        quality_score        * w.get("quality",       _DEFAULT_WEIGHTS["quality"])
        + performance_score  * w.get("performance",   _DEFAULT_WEIGHTS["performance"])
        + tag_overlap_score  * w.get("tag_overlap",   _DEFAULT_WEIGHTS["tag_overlap"])
        + style_match_score  * w.get("style_match",   _DEFAULT_WEIGHTS["style_match"])
        + provider_match_score * w.get("provider_match", _DEFAULT_WEIGHTS["provider_match"])
    )
    recency = _recency_multiplier(r.archived_at)
    final_score = max(0.0, min(1.0, raw_score * recency))

    return ReuseCandidate(
        asset_id           = r.asset_id,
        archive_id         = r.archive_id,
        episode_id         = r.episode_id,
        asset_type         = r.asset_type,
        provider           = r.provider,
        model              = r.model,
        quality_verdict    = r.quality_verdict,
        score              = final_score,
        score_components   = {
            "quality":       quality_score,
            "performance":   performance_score,
            "tag_overlap":   tag_overlap_score,
            "style_match":   style_match_score,
            "provider_match": provider_match_score,
        },
        recency_multiplier = recency,
        reuse_eligible     = r.reuse_eligible,
        semantic_tags      = r.semantic_tags,
        visual_style       = r.visual_style,
        performance_signal = r.performance_signal,
        archived_at        = r.archived_at,
    )


# ---------------------------------------------------------------------------
# Public API — recommend_reuse
# ---------------------------------------------------------------------------

def recommend_reuse(
    episode_id:    str,
    asset_type:    str,
    provider:      Optional[str]  = None,
    semantic_tags: Optional[list] = None,
    visual_style:  Optional[dict] = None,
    min_score:     float          = 0.5,
    limit:         int            = 10,
    db_path:       Optional[Path] = None,
    weights:       Optional[dict] = None,
) -> list[ReuseCandidate]:
    """
    Recommend archived assets that could be reused for a new generation slot.

    Queries archive_index for reuse-eligible assets of the requested type,
    scores each against the request context, filters below min_score,
    and returns up to `limit` candidates sorted descending by score.

    Assets from the same episode_id are included in the candidates —
    callers are responsible for deduplication if needed.

    Non-blocking: returns [] on any exception.

    Args:
        episode_id:    Episode requesting reuse (used for context, not filtered).
        asset_type:    Asset type to find candidates for (e.g. "video_clip").
        provider:      Preferred provider slug. Influences provider_match score.
        semantic_tags: Tags the new asset would have — used for tag_overlap score.
        visual_style:  Style dict for the new asset — used for style_match score.
        min_score:     Minimum score threshold [0.0–1.0]. Default 0.5.
        limit:         Maximum candidates to return. Default 10.
        db_path:       Override SQLite path.
        weights:       Override scoring weights dict.

    Returns:
        List of ReuseCandidate sorted descending by score. [] if none qualify.
    """
    try:
        recall_results = recall_assets(
            db_path=db_path,
            asset_type=asset_type,
            reuse_eligible_only=True,
            limit=500,  # fetch liberally; filter/rank in Python
        )
    except Exception:
        return []

    candidates: list[ReuseCandidate] = []
    for r in recall_results:
        try:
            cand = _score_to_candidate(
                r, semantic_tags, visual_style, provider, weights
            )
            if cand.score >= min_score:
                candidates.append(cand)
        except Exception:
            continue

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Public API — get_top_candidate
# ---------------------------------------------------------------------------

def get_top_candidate(
    episode_id:    str,
    asset_type:    str,
    provider:      Optional[str]  = None,
    semantic_tags: Optional[list] = None,
    visual_style:  Optional[dict] = None,
    min_score:     float          = 0.5,
    db_path:       Optional[Path] = None,
    weights:       Optional[dict] = None,
) -> Optional[ReuseCandidate]:
    """
    Return the single best reuse candidate, or None if none qualify.

    Calls recommend_reuse() with limit=1 and returns the first result,
    or None if the list is empty.

    Args:
        (same as recommend_reuse)

    Returns:
        Best-scoring ReuseCandidate, or None.
    """
    results = recommend_reuse(
        episode_id=episode_id,
        asset_type=asset_type,
        provider=provider,
        semantic_tags=semantic_tags,
        visual_style=visual_style,
        min_score=min_score,
        limit=1,
        db_path=db_path,
        weights=weights,
    )
    return results[0] if results else None


# ---------------------------------------------------------------------------
# Public API — should_reuse
# ---------------------------------------------------------------------------

def should_reuse(
    episode_id:    str,
    asset_type:    str,
    provider:      Optional[str]  = None,
    semantic_tags: Optional[list] = None,
    visual_style:  Optional[dict] = None,
    threshold:     float          = 0.6,
    db_path:       Optional[Path] = None,
    weights:       Optional[dict] = None,
) -> bool:
    """
    Decide whether an existing archived asset should be reused.

    Returns True if the top candidate's score >= threshold, False otherwise.
    Records the decision in the reuse_decisions table (non-blocking).

    Args:
        episode_id:    Episode requesting the asset.
        asset_type:    Asset type needed (e.g. "video_clip").
        provider:      Preferred provider.
        semantic_tags: Tags for the new asset.
        visual_style:  Style context for the new asset.
        threshold:     Minimum score to recommend reuse. Default 0.6.
        db_path:       Override SQLite path.
        weights:       Override scoring weights.

    Returns:
        True if reuse is recommended; False if new generation is recommended.
    """
    top = get_top_candidate(
        episode_id=episode_id,
        asset_type=asset_type,
        provider=provider,
        semantic_tags=semantic_tags,
        visual_style=visual_style,
        min_score=threshold,
        db_path=db_path,
        weights=weights,
    )
    decision = "reuse" if (top is not None and top.score >= threshold) else "generate"
    _record_decision(db_path, episode_id, asset_type, provider, top, decision)
    return decision == "reuse"
