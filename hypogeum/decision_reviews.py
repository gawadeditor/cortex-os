"""
hypogeum/decision_reviews.py — F4: Decision Review Persistence

Persists computed decisions as reviewable operator records.
Accept/Reject only records intent — no execution, no pipeline trigger.

Sync rules:
    - Only decisions with non-empty evidence are persisted.
    - If decision_id already exists:
        - pending: update title/recommendation/evidence/confidence
        - accepted/rejected: leave unchanged (preserve review history)
    - Duplicate syncs are idempotent.

Public API:
    initialize_decision_reviews_schema(db_path=None)  -> None
    sync_decisions_to_reviews(db_path=None)           -> dict
    list_decision_reviews(status=None, db_path=None)  -> list[dict]
    get_decision_review(review_id, db_path=None)      -> dict | None
    accept_decision(review_id, note="", db_path=None) -> dict
    reject_decision(review_id, note="", db_path=None) -> dict
"""

from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_VALID_STATUSES = frozenset({"pending", "accepted", "rejected"})


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    d = dict(row)
    # Decode evidence_json back to a list
    try:
        d["evidence"] = _json.loads(d.pop("evidence_json") or "[]")
    except Exception:
        d["evidence"] = []
    return d


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def initialize_decision_reviews_schema(db_path: Optional[Path] = None) -> None:
    """Ensure all F4 tables exist (idempotent via initialize_schema)."""
    initialize_schema(_resolve_db(db_path))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_by_decision_id(decision_id: str, db_path: Path) -> Optional[dict]:
    """Return the review row for a decision_id, or None."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM decision_reviews WHERE decision_id = ?",
            (decision_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _insert_review(decision: dict, db_path: Path) -> str:
    """Insert a new pending review. Returns the review_id."""
    review_id = str(uuid.uuid4())
    now = _now_iso()
    evidence_json = _json.dumps(decision.get("evidence") or [])

    conn = get_connection(db_path)
    try:
        conn.execute("""
            INSERT INTO decision_reviews
              (review_id, decision_id, decision_type, decision_title,
               recommendation, evidence_json, confidence,
               operator_status, operator_note, created_at, reviewed_at,
               metadata_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL, 'v1')
        """, (
            review_id,
            decision["decision_id"],
            decision["type"],
            decision["title"],
            decision["recommendation"],
            evidence_json,
            decision.get("confidence"),
            now,
        ))
        conn.commit()
    finally:
        conn.close()
    return review_id


def _update_pending_review(decision: dict, review_id: str, db_path: Path) -> None:
    """
    Update mutable fields of a pending review when the engine recomputes it.
    Preserves: review_id, decision_id, decision_type, operator_status, created_at.
    """
    evidence_json = _json.dumps(decision.get("evidence") or [])
    conn = get_connection(db_path)
    try:
        conn.execute("""
            UPDATE decision_reviews
            SET decision_title = ?,
                recommendation = ?,
                evidence_json  = ?,
                confidence     = ?
            WHERE review_id = ? AND operator_status = 'pending'
        """, (
            decision["title"],
            decision["recommendation"],
            evidence_json,
            decision.get("confidence"),
            review_id,
        ))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_decisions_to_reviews(db_path: Optional[Path] = None) -> dict:
    """
    Sync computed decisions from the decision engine into decision_reviews.

    Rules:
        - Decisions with empty evidence are skipped (no mock-derived decisions).
        - New decision_ids → insert as 'pending'.
        - Existing pending decision_ids → update title/recommendation/evidence/confidence.
        - Existing accepted/rejected decision_ids → skip (preserve history).

    Returns:
        {"ok": True, "created": n, "updated": n, "skipped": n,
         "total_decisions": n}
    """
    db = _resolve_db(db_path)
    initialize_schema(db)

    try:
        from hypogeum.decision_engine import get_decision_state
        ds = get_decision_state(db_path=db)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "code": "engine_error"}

    decisions = ds.get("decisions", [])
    created = updated = skipped = 0

    for d in decisions:
        # Guard: only persist decisions with real evidence
        if not d.get("evidence"):
            skipped += 1
            continue

        existing = _get_by_decision_id(d["decision_id"], db)

        if existing is None:
            _insert_review(d, db)
            created += 1
        elif existing["operator_status"] == "pending":
            _update_pending_review(d, existing["review_id"], db)
            updated += 1
        else:
            # accepted or rejected — preserve review history unchanged
            skipped += 1

    return {
        "ok":               True,
        "created":          created,
        "updated":          updated,
        "skipped":          skipped,
        "total_decisions":  len(decisions),
    }


def list_decision_reviews(
    status:  Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return decision reviews ordered: pending first, then by created_at DESC.

    Args:
        status: Filter by operator_status. None returns all.
    """
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            if status and status in _VALID_STATUSES:
                rows = conn.execute("""
                    SELECT * FROM decision_reviews
                    WHERE operator_status = ?
                    ORDER BY
                        CASE operator_status
                            WHEN 'pending'  THEN 0
                            WHEN 'accepted' THEN 1
                            WHEN 'rejected' THEN 2
                        END,
                        created_at DESC
                """, (status,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM decision_reviews
                    ORDER BY
                        CASE operator_status
                            WHEN 'pending'  THEN 0
                            WHEN 'accepted' THEN 1
                            WHEN 'rejected' THEN 2
                        END,
                        created_at DESC
                """).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def get_decision_review(
    review_id: str,
    db_path:   Optional[Path] = None,
) -> Optional[dict]:
    """Return one review by review_id, or None if not found."""
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT * FROM decision_reviews WHERE review_id = ?",
                (review_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_dict(row) if row else None
    except Exception:
        return None


def accept_decision(
    review_id: str,
    note:      str = "",
    db_path:   Optional[Path] = None,
) -> dict:
    """
    Mark a decision review as accepted.

    Records operator intent only. No pipeline action is triggered.

    Returns:
        {"ok": True, "review_id": ..., "status": "accepted"}
        {"ok": False, "code": "not_found"}
    """
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        review = get_decision_review(review_id, db_path=db)
        if review is None:
            return {"ok": False, "code": "not_found",
                    "error": f"Review {review_id!r} not found."}

        now = _now_iso()
        conn = get_connection(db)
        try:
            conn.execute("""
                UPDATE decision_reviews
                SET operator_status = 'accepted',
                    operator_note   = ?,
                    reviewed_at     = ?
                WHERE review_id = ?
            """, (note or None, now, review_id))
            conn.commit()
        finally:
            conn.close()

        return {"ok": True, "review_id": review_id, "status": "accepted",
                "reviewed_at": now}
    except Exception as exc:
        return {"ok": False, "code": "error", "error": str(exc)}


def reject_decision(
    review_id: str,
    note:      str = "",
    db_path:   Optional[Path] = None,
) -> dict:
    """
    Mark a decision review as rejected.

    Records operator intent only. No pipeline action is triggered.

    Returns:
        {"ok": True, "review_id": ..., "status": "rejected"}
        {"ok": False, "code": "not_found"}
    """
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        review = get_decision_review(review_id, db_path=db)
        if review is None:
            return {"ok": False, "code": "not_found",
                    "error": f"Review {review_id!r} not found."}

        now = _now_iso()
        conn = get_connection(db)
        try:
            conn.execute("""
                UPDATE decision_reviews
                SET operator_status = 'rejected',
                    operator_note   = ?,
                    reviewed_at     = ?
                WHERE review_id = ?
            """, (note or None, now, review_id))
            conn.commit()
        finally:
            conn.close()

        return {"ok": True, "review_id": review_id, "status": "rejected",
                "reviewed_at": now}
    except Exception as exc:
        return {"ok": False, "code": "error", "error": str(exc)}
