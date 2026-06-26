"""
hypogeum/discovery/repository.py — Opportunity Repository v2

The sole writer for the three v2 discovery tables:
    opportunities        — one row per v2 candidate, full lifecycle
    opportunity_events   — append-only audit log per opportunity
    discovery_cycles     — one row per v2 Director cycle, funnel metrics

v1 heuristic cycles write only to topic_candidates; this module never
touches that table and imposes no ordering constraint on v1 runs.

Design contracts:
  - No API calls, no scoring logic, no queue logic.
  - opportunity_id is the primary identity key (UUID4, assigned here).
  - canonical_subject is an indexed subject field, not a surrogate key.
  - Every update_opportunity() call appends one event in the same transaction.
  - All public functions return structured dicts and never raise.
  - Missing optional fields are inserted as NULL; they never crash.

Public API:
    start_cycle(cycle_id, channel_id, engine_used="v2", db_path=None) -> dict
    save_opportunities(opportunities, cycle_id, db_path=None) -> dict
    record_event(opportunity_id, event_type, actor, detail=None,
                 cycle_id=None, channel_id=None, db_path=None) -> dict
    update_opportunity(opportunity_id, fields, event_type, actor,
                       detail=None, db_path=None) -> dict
    complete_cycle(cycle_id, summary, db_path=None) -> dict
    fail_cycle(cycle_id, reason, db_path=None) -> dict
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _detail_json(actor: str, detail: Optional[str]) -> str:
    payload: dict = {"actor": actor}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload)


# Whitelisted columns for opportunistic UPDATE; prevents SQL injection from caller-supplied keys.
_UPDATABLE_OPP_FIELDS = frozenset({
    "status", "verification_status", "source_count", "source_score",
    "verify_count", "initial_composite", "final_composite", "priority_score",
    "priority_tier", "rejection_reason", "topic_id", "routed_to",
    "selected_at", "enqueued_at", "last_seen_at",
})

# Whitelisted columns for discovery_cycles UPDATE.
_UPDATABLE_CYCLE_FIELDS = frozenset({
    "raw_candidates_count", "viable_after_feasibility", "verify_eligible_count",
    "verified_count", "rejected_source_count", "rejected_score_count",
    "selected_opportunity_id", "selected_canonical_subject", "topic_id_enqueued",
    "model_used", "prompt_version",
})

# Valid values for opportunities.verification_status CHECK constraint.
_VALID_VERIFICATION_STATUSES = frozenset({"unverified", "verified", "failed", "skipped"})


def _coerce_verification_status(raw: object) -> str:
    if raw in _VALID_VERIFICATION_STATUSES:
        return str(raw)
    return "unverified"


# ---------------------------------------------------------------------------
# start_cycle
# ---------------------------------------------------------------------------

def start_cycle(
    cycle_id: str,
    channel_id: str,
    engine_used: str = "v2",
    db_path: Optional[Path] = None,
) -> dict:
    """
    Insert a new discovery_cycles row with status='running'.

    Returns:
        {ok: True, cycle_id, channel_id, engine_used} on success.
        {ok: False, failure_reason: "duplicate_cycle"} if cycle_id exists.
        {ok: False, failure_reason: "error", error: str} on any other exception.
    """
    try:
        conn = get_connection(db_path)
        try:
            conn.execute(
                """INSERT INTO discovery_cycles
                   (cycle_id, channel_id, engine_used, status, created_at)
                   VALUES (?, ?, ?, 'running', ?)""",
                (cycle_id, channel_id, engine_used, _now()),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok":         True,
            "cycle_id":   cycle_id,
            "channel_id": channel_id,
            "engine_used": engine_used,
        }
    except sqlite3.IntegrityError:
        return {"ok": False, "failure_reason": "duplicate_cycle"}
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# save_opportunities
# ---------------------------------------------------------------------------

def save_opportunities(
    opportunities: list[dict],
    cycle_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Persist enriched opportunities and append a 'discovered' event per row.

    Assigns a fresh opportunity_id (UUID4) to each entry — callers must not
    supply their own IDs. The list of assigned IDs is returned so the caller
    can reference them in subsequent update_opportunity() calls.

    Returns:
        {ok: True, saved_count, opportunity_ids: [str]} on success.
        {ok: False, failure_reason: "error", error: str} on failure.
    """
    try:
        now = _now()
        conn = get_connection(db_path)
        saved_ids: list[str] = []
        try:
            for opp in opportunities:
                opp_id = _new_id()
                saved_ids.append(opp_id)

                conn.execute(
                    """INSERT INTO opportunities (
                         opportunity_id, canonical_subject,
                         topic_text, angle, mystery_hook,
                         differentiation_claim, visual_signal, evidence_signal,
                         generation_source,
                         first_seen_at, last_seen_at,
                         verification_status, source_count, source_score,
                         verify_count, initial_composite, final_composite,
                         priority_score, priority_tier, status
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        opp_id,
                        opp.get("canonical_subject") or "",
                        opp.get("topic_text") or "",
                        opp.get("angle"),
                        opp.get("mystery_hook"),
                        opp.get("differentiation_claim"),
                        opp.get("visual_signal"),
                        opp.get("evidence_signal"),
                        opp.get("generation_source"),
                        now,
                        now,
                        _coerce_verification_status(opp.get("verification_status")),
                        int(opp.get("source_count") or 0),
                        opp.get("source_score"),
                        0,
                        opp.get("initial_composite"),
                        opp.get("final_composite"),
                        opp.get("priority_score"),
                        opp.get("priority_tier"),
                        "active",
                    ),
                )
                conn.execute(
                    """INSERT INTO opportunity_events
                       (event_id, opportunity_id, event_type, cycle_id,
                        channel_id, detail_json, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        _new_id(),
                        opp_id,
                        "discovered",
                        cycle_id,
                        None,
                        json.dumps({
                            "actor":      "topic_ideator",
                            "topic_text": opp.get("topic_text"),
                        }),
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok":              True,
            "saved_count":     len(saved_ids),
            "opportunity_ids": saved_ids,
        }
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------

def record_event(
    opportunity_id: str,
    event_type: str,
    actor: str,
    detail: Optional[str] = None,
    cycle_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Append a single audit event for an opportunity.

    actor and detail are serialised into detail_json (no dedicated column).

    Returns:
        {ok: True, event_id, opportunity_id, event_type} on success.
        {ok: False, failure_reason: "error", error: str} on failure.
    """
    try:
        event_id = _new_id()
        conn = get_connection(db_path)
        try:
            conn.execute(
                """INSERT INTO opportunity_events
                   (event_id, opportunity_id, event_type, cycle_id,
                    channel_id, detail_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    event_id,
                    opportunity_id,
                    event_type,
                    cycle_id,
                    channel_id,
                    _detail_json(actor, detail),
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok":             True,
            "event_id":       event_id,
            "opportunity_id": opportunity_id,
            "event_type":     event_type,
        }
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# update_opportunity
# ---------------------------------------------------------------------------

def update_opportunity(
    opportunity_id: str,
    fields: dict,
    event_type: str,
    actor: str,
    detail: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Atomically update an opportunity row and append one audit event.

    Only whitelisted field names are accepted. Unknown fields are silently
    dropped. last_seen_at is always refreshed regardless of the fields dict.
    Both the UPDATE and the INSERT share one commit — they succeed or fail
    together.

    Returns:
        {ok: True, opportunity_id, fields_updated: [str], event_id} on success.
        {ok: False, failure_reason: "error", error: str} on failure.
    """
    try:
        safe = {k: v for k, v in fields.items() if k in _UPDATABLE_OPP_FIELDS}
        safe["last_seen_at"] = _now()

        set_clause = ", ".join(f"{k} = ?" for k in safe)
        set_values = list(safe.values()) + [opportunity_id]

        event_id = _new_id()
        now = _now()

        conn = get_connection(db_path)
        try:
            conn.execute(
                f"UPDATE opportunities SET {set_clause} WHERE opportunity_id = ?",
                set_values,
            )
            conn.execute(
                """INSERT INTO opportunity_events
                   (event_id, opportunity_id, event_type, cycle_id,
                    channel_id, detail_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (event_id, opportunity_id, event_type, None, None,
                 _detail_json(actor, detail), now),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "ok":              True,
            "opportunity_id":  opportunity_id,
            "fields_updated":  list(safe.keys()),
            "event_id":        event_id,
        }
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# complete_cycle
# ---------------------------------------------------------------------------

def complete_cycle(
    cycle_id: str,
    summary: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Mark a cycle complete and write funnel metrics from summary.

    Accepted summary keys: raw_candidates_count, viable_after_feasibility,
    verify_eligible_count, verified_count, rejected_source_count,
    rejected_score_count, selected_opportunity_id, selected_canonical_subject,
    topic_id_enqueued, model_used, prompt_version.
    Extra keys are ignored.

    Returns:
        {ok: True, cycle_id} on success.
        {ok: False, failure_reason: "error", error: str} on failure.
    """
    try:
        safe = {k: v for k, v in summary.items() if k in _UPDATABLE_CYCLE_FIELDS}
        safe["status"] = "complete"
        safe["completed_at"] = _now()

        set_clause = ", ".join(f"{k} = ?" for k in safe)
        values = list(safe.values()) + [cycle_id]

        conn = get_connection(db_path)
        try:
            conn.execute(
                f"UPDATE discovery_cycles SET {set_clause} WHERE cycle_id = ?",
                values,
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "cycle_id": cycle_id}
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# fail_cycle
# ---------------------------------------------------------------------------

def fail_cycle(
    cycle_id: str,
    reason: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Mark a cycle as failed and record completed_at.

    The reason string is not persisted (no dedicated column in the schema);
    it is available in the return dict for immediate logging by the caller.

    Returns:
        {ok: True, cycle_id, reason} on success.
        {ok: False, failure_reason: "error", error: str} on failure.
    """
    try:
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE discovery_cycles SET status='failed', completed_at=? WHERE cycle_id=?",
                (_now(), cycle_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "cycle_id": cycle_id, "reason": reason}
    except Exception as exc:
        return {"ok": False, "failure_reason": "error", "error": str(exc)}
