"""
hypogeum/discovery/source_verifier.py — Source Verifier v2

Verifies score-qualified discovery candidates via Perplexity.
Called after topic_ideator.run_topic_ideator() in the Discovery Director pipeline.

Verification pipeline per run_source_verifier() call:
  1. Compute best_score = max initial_composite across all candidates.
  2. For each candidate:
       not score-qualified  → verification_status="skipped" (no DB write)
       cache hit            → return cached result (no Perplexity call)
       cache miss           → call query_perplexity(), persist if sources found
  3. Return full candidate list with verification fields attached.

Score qualification (no fixed top-N):
  A candidate is verify-eligible when:
    initial_composite >= DISCOVERY_VERIFY_THRESHOLD   (absolute floor)
    OR initial_composite >= best_score - DISCOVERY_VERIFY_DELTA  (within delta of best)

Cache contract:
  Keyed on canonical_subject. Only successful verifications (source_count >= 1)
  are persisted. Failed calls are not cached — the next cycle will retry.

Output fields added per candidate:
  verification_status  "verified" | "failed" | "skipped"
  source_count         int
  source_score         float [0.0, 1.0]  — min(1.0, source_count / 3)
  source_urls          list[str]
  verification_id      str (present when result comes from DB — new or cached)

Public API:
  run_source_verifier(candidates, config, cycle_id=None, db_path=None) -> list[dict]
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import get_connection
from hypogeum.perplexity import query_perplexity
from hypogeum.prompt_loader import load_prompt


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VERIFY_QUERY_TEMPLATE = load_prompt(
    Path(__file__).resolve().parent.parent / "prompts" / "source_verifier_query.txt"
)

_SOURCES_FOR_FULL_SCORE = 3


def _source_score(source_count: int) -> float:
    """Linear score: 0 sources → 0.0, _SOURCES_FOR_FULL_SCORE+ sources → 1.0."""
    if source_count == 0:
        return 0.0
    return round(min(1.0, source_count / _SOURCES_FOR_FULL_SCORE), 4)


def _confidence(source_count: int) -> str:
    if source_count >= _SOURCES_FOR_FULL_SCORE:
        return "high"
    if source_count >= 1:
        return "medium"
    return "low"


def _is_qualified(candidate: dict, best_score: float, threshold: float, delta: float) -> bool:
    """True when the candidate meets the absolute or relative score gate."""
    score = candidate.get("initial_composite", 0.0) or 0.0
    return score >= threshold or score >= best_score - delta


def _cache_lookup(canonical_subject: str, conn) -> Optional[dict]:
    """
    Return a cached verification result for this canonical_subject, or None.
    Only rows written by _persist (verified=1) exist; any hit means verified.
    """
    row = conn.execute(
        """SELECT verification_id, source_url
           FROM source_verifications
           WHERE canonical_subject = ? AND verified = 1
           ORDER BY created_at DESC LIMIT 1""",
        (canonical_subject,),
    ).fetchone()
    if row is None:
        return None
    verification_id, source_url_raw = row
    try:
        source_urls = json.loads(source_url_raw) if source_url_raw else []
    except (json.JSONDecodeError, TypeError):
        source_urls = [source_url_raw] if source_url_raw else []
    source_count = len(source_urls)
    return {
        "verification_id":    verification_id,
        "verification_status": "verified",
        "source_count":       source_count,
        "source_score":       _source_score(source_count),
        "source_urls":        source_urls,
    }


def _persist(
    canonical_subject: str,
    cycle_id: Optional[str],
    source_urls: list[str],
    conn,
) -> str:
    """Write a verified row (source_count >= 1) and return verification_id."""
    vid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO source_verifications
           (verification_id, canonical_subject, cycle_id, verified, source_url,
            confidence, created_at)
           VALUES (?, ?, ?, 1, ?, ?, ?)""",
        (
            vid,
            canonical_subject,
            cycle_id,
            json.dumps(source_urls),
            _confidence(len(source_urls)),
            now,
        ),
    )
    conn.commit()
    return vid


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_source_verifier(
    candidates: list[dict],
    config: Config,
    cycle_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Verify score-qualified candidates via Perplexity.

    Args:
        candidates:  Enriched opportunities from topic_ideator (must have
                     canonical_subject and initial_composite).
        config:      Loaded Config instance.
        cycle_id:    Optional discovery cycle identifier for DB rows.
        db_path:     DB path override (for tests).

    Returns:
        Same list with verification_status, source_count, source_score,
        source_urls, and (when persisted/cached) verification_id added.
    """
    if not candidates:
        return []

    threshold = float(config.DISCOVERY_VERIFY_THRESHOLD or 0.65)
    delta = float(config.DISCOVERY_VERIFY_DELTA or 0.10)
    best_score = max(
        (c.get("initial_composite", 0.0) or 0.0) for c in candidates
    )

    conn = get_connection(db_path)
    results: list[dict] = []

    try:
        for candidate in candidates:
            canonical = candidate.get("canonical_subject") or ""
            topic_text = candidate.get("topic_text") or canonical

            # ── Qualification gate ─────────────────────────────────────────
            if not _is_qualified(candidate, best_score, threshold, delta):
                results.append({
                    **candidate,
                    "verification_status": "skipped",
                    "source_count":        0,
                    "source_score":        0.0,
                    "source_urls":         [],
                })
                continue

            # ── Cache check ────────────────────────────────────────────────
            if canonical:
                cached = _cache_lookup(canonical, conn)
                if cached is not None:
                    results.append({**candidate, **cached})
                    continue

            # ── Perplexity call ────────────────────────────────────────────
            try:
                plex = query_perplexity(
                    _VERIFY_QUERY_TEMPLATE.format(topic_text=topic_text),
                    config,
                    phase_id="source_verification",
                    episode_id=cycle_id,
                    agent="source_verifier",
                )
                source_urls = list(plex.citations)

                if source_urls and canonical:
                    vid = _persist(canonical, cycle_id, source_urls, conn)
                    results.append({
                        **candidate,
                        "verification_status": "verified",
                        "source_count":        len(source_urls),
                        "source_score":        _source_score(len(source_urls)),
                        "source_urls":         source_urls,
                        "verification_id":     vid,
                    })
                else:
                    # Perplexity succeeded but returned no citations — not cached
                    results.append({
                        **candidate,
                        "verification_status": "failed",
                        "source_count":        0,
                        "source_score":        0.0,
                        "source_urls":         [],
                    })

            except Exception:
                # Network/auth/parse error — mark failed, do not cache, continue
                results.append({
                    **candidate,
                    "verification_status": "failed",
                    "source_count":        0,
                    "source_score":        0.0,
                    "source_urls":         [],
                })

    finally:
        conn.close()

    return results
