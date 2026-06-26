"""
hypogeum/agents/discovery_director.py — Discovery Director v2 orchestrator

Coordinates the full v2 topic discovery pipeline for one cycle:

  1. start_cycle()         — open a discovery_cycles row
  2. Topic Ideator         — LLM-generated candidates (with built-in seed fallback)
  3. Source Verifier       — Perplexity verification (graceful on failure)
  4. save_opportunities()  — persist all candidates to the repository
  5. select winner         — highest scoring eligible candidate
  6. update_opportunity()  — mark selected
  7. route_selected_opportunity() — enqueue or hold for approval
  8. update_opportunity()  — mark enqueued / pending_approval
  9. complete_cycle()      — write funnel metrics

Failure contract:
  - Never raises. All exceptions are caught; failures return structured dicts.
  - Topic Ideator failure → its own built-in fallback (seed candidates).
  - Source Verifier failure → continue with unverified candidates.
  - Repository failure → fail_cycle() + return {ok: False}.
  - Every other unexpected exception → fail_cycle() + return {ok: False}.

Winner selection rules (step 5):
  - Candidates with verification_status="failed" are excluded when at least
    one "verified" candidate exists. If no verified candidates exist, failed
    candidates are eligible.
  - Among eligible candidates, sort by initial_composite desc, source_score desc.
  - Pick the top-ranked candidate.

Public API:
  run(channel_id, config=None, db_path=None, triggered_by="worker_refill") -> dict

Return shape:
  {ok, engine_used, cycle_id, channel_id, candidate_count, verified_count,
   selected_opportunity_id, topic_id_enqueued, route_status, opportunities}
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.discovery import repository
from hypogeum.discovery.queue_gateway import route_selected_opportunity
from hypogeum.discovery.source_verifier import run_source_verifier
from hypogeum.discovery.topic_ideator import run_topic_ideator

_ENGINE = "v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count(candidates: list[dict], status: str) -> int:
    return sum(1 for c in candidates if c.get("verification_status") == status)


def _select_winner(candidates: list[dict]) -> Optional[dict]:
    """
    Return the best eligible candidate, or None if the list is empty.

    Eligibility: "failed" candidates are excluded when at least one
    "verified" candidate exists. "skipped" and "unverified" are always eligible.
    """
    if not candidates:
        return None

    verified_exists = any(c.get("verification_status") == "verified" for c in candidates)

    eligible = [
        c for c in candidates
        if not (c.get("verification_status") == "failed" and verified_exists)
    ]

    if not eligible:
        eligible = candidates  # safety: if all were filtered, relax constraint

    eligible.sort(
        key=lambda c: (
            c.get("initial_composite") or 0.0,
            c.get("source_score") or 0.0,
        ),
        reverse=True,
    )
    return eligible[0]


def run(
    channel_id: str,
    config=None,
    db_path: Optional[Path] = None,
    triggered_by: str = "worker_refill",
) -> dict:
    """
    Execute one full v2 discovery cycle for channel_id.

    Args:
        channel_id:   Target channel identifier.
        config:       Loaded Config instance. May be None (ideator falls back to seeds).
        db_path:      DB path override (for tests).
        triggered_by: Caller tag recorded in cycle metadata (informational).

    Returns:
        {ok, engine_used, cycle_id, channel_id, candidate_count, verified_count,
         selected_opportunity_id, topic_id_enqueued, route_status, opportunities}
    """
    cycle_id = str(uuid.uuid4())

    _base = {
        "engine_used":             _ENGINE,
        "cycle_id":                cycle_id,
        "channel_id":              channel_id,
        "candidate_count":         0,
        "verified_count":          0,
        "selected_opportunity_id": None,
        "topic_id_enqueued":       None,
        "route_status":            "no_candidate",
        "opportunities":           [],
    }

    # ── 1. Start cycle ────────────────────────────────────────────────────────
    try:
        start_result = repository.start_cycle(
            cycle_id, channel_id, engine_used=_ENGINE, db_path=db_path
        )
        if not start_result.get("ok"):
            return {**_base, "ok": False,
                    "failure_reason": start_result.get("failure_reason", "cycle_start_failed")}
    except Exception as exc:
        return {**_base, "ok": False, "failure_reason": "cycle_start_error", "error": str(exc)}

    try:
        # ── 2. Topic Ideator ──────────────────────────────────────────────────
        try:
            ideator_out = run_topic_ideator(
                channel_id, config, db_path=db_path
            )
            candidates: list[dict] = list(ideator_out.get("opportunities", []))
        except Exception:
            # Ideator itself has a fallback, but if the whole call fails, treat
            # as empty — cycle continues with no candidates.
            candidates = []

        raw_count = len(candidates)

        # ── 3. Source Verifier ────────────────────────────────────────────────
        try:
            candidates = run_source_verifier(
                candidates, config, cycle_id=cycle_id, db_path=db_path
            )
        except Exception:
            # Verifier failed entirely — continue with unverified candidates.
            for c in candidates:
                c.setdefault("verification_status", "unverified")
                c.setdefault("source_count", 0)
                c.setdefault("source_score", 0.0)
                c.setdefault("source_urls", [])

        # Stamp channel_id onto every candidate for routing
        for c in candidates:
            c["channel_id"] = channel_id

        # ── 4. Save opportunities ─────────────────────────────────────────────
        if candidates:
            save_result = repository.save_opportunities(
                candidates, cycle_id, db_path=db_path
            )
            if not save_result.get("ok"):
                repository.fail_cycle(cycle_id, "save_failed", db_path=db_path)
                return {**_base, "ok": False, "failure_reason": "save_failed",
                        "candidate_count": raw_count}

            # Map assigned opportunity_ids back to candidate dicts
            assigned_ids: list[str] = save_result.get("opportunity_ids", [])
            for i, c in enumerate(candidates):
                if i < len(assigned_ids):
                    c["_opportunity_id"] = assigned_ids[i]

        # ── Funnel counters ───────────────────────────────────────────────────
        verified_count    = _count(candidates, "verified")
        failed_count      = _count(candidates, "failed")
        skipped_count     = _count(candidates, "skipped")
        verify_eligible   = verified_count + failed_count  # Perplexity was attempted
        rejected_source   = failed_count

        # ── 5. Select winner ──────────────────────────────────────────────────
        winner          = _select_winner(candidates)
        winner_opp_id   = winner.get("_opportunity_id") if winner else None

        # ── 6 & 7. Route and update opportunity status ────────────────────────
        topic_id_enqueued = None
        route_status      = "no_candidate"

        if winner and winner_opp_id:
            # Mark as selected
            repository.update_opportunity(
                winner_opp_id,
                {"status": "selected", "selected_at": _now()},
                event_type="consumed",
                actor="discovery_director",
                detail="selected as cycle winner",
                db_path=db_path,
            )

            # Route (queue_gateway handles auto_approve logic)
            try:
                route_result = route_selected_opportunity(
                    winner, config, db_path=db_path
                )
            except Exception:
                route_result = {"ok": False, "failure_reason": "route_exception"}

            routed_to = route_result.get("routed_to", "")
            route_status = routed_to or route_result.get("failure_reason", "error")

            if route_result.get("ok") and routed_to == "episode_topics":
                # ── 8. Enqueued ───────────────────────────────────────────────
                topic_id_enqueued = route_result.get("topic_id")
                priority = route_result.get("queue_priority", 5)
                repository.update_opportunity(
                    winner_opp_id,
                    {
                        "status":        "enqueued",
                        "topic_id":      topic_id_enqueued,
                        "routed_to":     channel_id,
                        "enqueued_at":   _now(),
                        "priority_score": float(priority) / 10.0,
                    },
                    event_type="consumed",
                    actor="queue_gateway",
                    detail=f"enqueued priority={priority}",
                    db_path=db_path,
                )

            elif route_result.get("ok") and routed_to == "pending_approval":
                # ── 9. Pending — status stays selected ────────────────────────
                route_status = "pending_approval"

        # ── 10. Complete cycle ────────────────────────────────────────────────
        repository.complete_cycle(
            cycle_id,
            {
                "raw_candidates_count":     raw_count,
                "viable_after_feasibility": raw_count,
                "verify_eligible_count":    verify_eligible,
                "verified_count":           verified_count,
                "rejected_source_count":    rejected_source,
                "rejected_score_count":     skipped_count,
                "selected_opportunity_id":  winner_opp_id,
                "selected_canonical_subject": (
                    winner.get("canonical_subject") if winner else None
                ),
                "topic_id_enqueued": topic_id_enqueued,
            },
            db_path=db_path,
        )

        return {
            "ok":                      True,
            "engine_used":             _ENGINE,
            "cycle_id":                cycle_id,
            "channel_id":              channel_id,
            "candidate_count":         raw_count,
            "verified_count":          verified_count,
            "selected_opportunity_id": winner_opp_id,
            "topic_id_enqueued":       topic_id_enqueued,
            "route_status":            route_status,
            "opportunities":           candidates,
        }

    except Exception as exc:
        # Last-resort catch — never raise from the director
        repository.fail_cycle(cycle_id, f"unexpected: {exc}", db_path=db_path)
        return {**_base, "ok": False, "failure_reason": "unexpected_error", "error": str(exc)}
