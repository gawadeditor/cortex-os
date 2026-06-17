"""
hypogeum/decision_execution.py — F5: Decision Execution Guardrails

Converts accepted decision_reviews into dry-run / reviewable execution plans.

HARD CONSTRAINTS (F5):
    - No episode launch.
    - No publishing.
    - No provider / API calls.
    - No paid calls.
    - No external network calls.
    - No automatic execution.
    - plan_status='executed' is UNREACHABLE in F5.
    - execution_mode='live' is BLOCKED in F5.
    - Everything is auditable and reversible.

Public API:
    create_execution_plan_from_review(review_id, db_path=None)  -> dict
    list_execution_plans(status=None, db_path=None)             -> list[dict]
    get_execution_plan(plan_id, db_path=None)                   -> dict | None
    approve_execution_plan(plan_id, note="", db_path=None)      -> dict
    reject_execution_plan(plan_id, note="", db_path=None)       -> dict
    cancel_execution_plan(plan_id, note="", db_path=None)       -> dict
    dry_run_execution_plan(plan_id, db_path=None)               -> dict
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

_VALID_STATUSES = frozenset({"draft", "approved", "rejected", "cancelled"})

# ---------------------------------------------------------------------------
# Plan templates by decision_type
# ---------------------------------------------------------------------------

_PLAN_TEMPLATES: dict[str, dict] = {
    "launch_readiness": {
        "action_summary": "Verify all conditions required for episode launch.",
        "steps": [
            "Verify run clearance from factory director.",
            "Verify required credentials are configured and valid.",
            "Verify all pending approvals are cleared.",
            "Verify a queued topic exists in the topic queue.",
            "Prepare launch command preview (dry-run only).",
        ],
        "risk_level": "high",
        "blockers": ["Live launch disabled in F5"],
    },
    "failure_review": {
        "action_summary": "Investigate recent pipeline failures and recommend remediation.",
        "steps": [
            "Inspect recent failed runs.",
            "Open failed run details.",
            "Collect common failure causes.",
            "Recommend operator investigation path.",
        ],
        "risk_level": "medium",
        "blockers": [],
    },
    "cost_review": {
        "action_summary": "Identify high-cost providers/models and produce an optimisation checklist.",
        "steps": [
            "Read cost aggregates from decision inputs.",
            "Identify the highest-cost provider and model.",
            "Produce optimisation checklist.",
        ],
        "risk_level": "low",
        "blockers": [],
    },
    "publishing_gap": {
        "action_summary": "Identify completed episodes without a publishing outcome.",
        "steps": [
            "Check completed episodes without a publishing outcome.",
            "Verify publishing profiles are configured.",
            "Produce publishing readiness checklist.",
        ],
        "risk_level": "high",
        "blockers": ["Publishing execution disabled in F5"],
    },
    "audience_review": {
        "action_summary": "Summarise real audience data and separate from mock/demo data.",
        "steps": [
            "Read real non-mock audience snapshots from decision inputs.",
            "Separate mock/demo data from real data.",
            "Produce evidence summary.",
        ],
        "risk_level": "low",
        "blockers": [],
    },
    "discovery_review": {
        "action_summary": "Assess topic discovery queue depth and propose actions.",
        "steps": [
            "Read topic queue from decision inputs.",
            "Identify queue depth and oldest topic.",
            "Produce discovery queue action checklist.",
        ],
        "risk_level": "low",
        "blockers": [],
    },
}

_UNKNOWN_TEMPLATE = {
    "action_summary": "Unknown decision type — no plan template available.",
    "steps": [
        "Identify the decision type and add a plan template.",
        "Re-sync decisions and create a new plan.",
    ],
    "risk_level": "blocked",
    "blockers": ["Unsupported decision type — plan template not found"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("planned_steps_json", "blockers_json", "evidence_json"):
        key = field.replace("_json", "s") if field == "planned_steps_json" else field.replace("_json", "")
        # Normalise: planned_steps_json → planned_steps, blockers_json → blockers, evidence_json → evidence
        raw_key = {
            "planned_steps_json": "planned_steps",
            "blockers_json":      "blockers",
            "evidence_json":      "evidence",
        }[field]
        try:
            d[raw_key] = _json.loads(d.pop(field) or "[]")
        except Exception:
            d[raw_key] = []
    return d


def _get_review(review_id: str, db: Path) -> Optional[dict]:
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT * FROM decision_reviews WHERE review_id = ?",
            (review_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["evidence"] = _json.loads(d.pop("evidence_json") or "[]")
        except Exception:
            d["evidence"] = []
        return d
    finally:
        conn.close()


def _get_plan_by_review_id(review_id: str, db: Path) -> Optional[dict]:
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT * FROM decision_execution_plans WHERE review_id = ?",
            (review_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _build_plan(review: dict) -> dict:
    """Derive plan fields from a review record. No I/O."""
    dtype = review.get("decision_type", "")
    tmpl  = _PLAN_TEMPLATES.get(dtype, _UNKNOWN_TEMPLATE)
    return {
        "action_summary": tmpl["action_summary"],
        "steps":          tmpl["steps"],
        "risk_level":     tmpl["risk_level"],
        "blockers":       list(tmpl["blockers"]),
        "evidence":       review.get("evidence") or [],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_execution_plan_from_review(
    review_id: str,
    db_path:   Optional[Path] = None,
) -> dict:
    """
    Create a dry-run execution plan from an accepted decision_review.

    Rules:
        - review must exist and have operator_status='accepted'.
        - If a plan already exists for this review_id, return it (idempotent).
        - execution_mode is always 'dry_run' in F5.
        - plan_status starts as 'draft'.

    Returns:
        {"ok": True, "plan_id": ..., "created": bool}
        {"ok": False, "code": ..., "error": ...}
    """
    db = _resolve_db(db_path)
    initialize_schema(db)

    review = _get_review(review_id, db)
    if review is None:
        return {"ok": False, "code": "review_not_found",
                "error": f"Decision review {review_id!r} not found."}

    status = review.get("operator_status")
    if status == "pending":
        return {"ok": False, "code": "review_not_accepted",
                "error": "Review is still pending. Accept it before creating an execution plan."}
    if status == "rejected":
        return {"ok": False, "code": "review_rejected",
                "error": "Review was rejected. Cannot create an execution plan from a rejected review."}

    # Idempotent: return existing plan if one already exists for this review
    existing = _get_plan_by_review_id(review_id, db)
    if existing is not None:
        return {"ok": True, "plan_id": existing["plan_id"], "created": False,
                "plan": existing}

    plan_fields = _build_plan(review)
    plan_id     = str(uuid.uuid4())
    now         = _now_iso()

    conn = get_connection(db)
    try:
        conn.execute("""
            INSERT INTO decision_execution_plans
              (plan_id, review_id, decision_id, decision_type,
               created_at, plan_status, execution_mode,
               action_summary, planned_steps_json,
               risk_level, blockers_json, evidence_json,
               operator_note, approved_at, rejected_at, executed_at,
               metadata_version)
            VALUES (?, ?, ?, ?, ?, 'draft', 'dry_run', ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 'v1')
        """, (
            plan_id,
            review_id,
            review["decision_id"],
            review["decision_type"],
            now,
            plan_fields["action_summary"],
            _json.dumps(plan_fields["steps"]),
            plan_fields["risk_level"],
            _json.dumps(plan_fields["blockers"]),
            _json.dumps(plan_fields["evidence"]),
        ))
        conn.commit()
    finally:
        conn.close()

    plan = get_execution_plan(plan_id, db_path=db)
    return {"ok": True, "plan_id": plan_id, "created": True, "plan": plan}


def list_execution_plans(
    status:  Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return execution plans ordered: draft first, then approved, then others,
    within each group by created_at DESC.

    Args:
        status: Filter by plan_status. None returns all.
    """
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            order = """
                ORDER BY
                    CASE plan_status
                        WHEN 'draft'     THEN 0
                        WHEN 'approved'  THEN 1
                        WHEN 'rejected'  THEN 2
                        WHEN 'cancelled' THEN 3
                        WHEN 'executed'  THEN 4
                    END,
                    created_at DESC
            """
            if status and status in frozenset({"draft", "approved", "rejected", "cancelled", "executed"}):
                rows = conn.execute(
                    f"SELECT * FROM decision_execution_plans WHERE plan_status = ? {order}",
                    (status,)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM decision_execution_plans {order}"
                ).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def get_execution_plan(
    plan_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return one plan by plan_id, or None."""
    db = _resolve_db(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT * FROM decision_execution_plans WHERE plan_id = ?",
                (plan_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_dict(row) if row else None
    except Exception:
        return None


def _transition_plan(
    plan_id:    str,
    new_status: str,
    note:       str,
    ts_field:   str,
    db:         Path,
) -> dict:
    """Generic status transition helper."""
    initialize_schema(db)
    plan = get_execution_plan(plan_id, db_path=db)
    if plan is None:
        return {"ok": False, "code": "not_found",
                "error": f"Plan {plan_id!r} not found."}

    now = _now_iso()
    conn = get_connection(db)
    try:
        conn.execute(f"""
            UPDATE decision_execution_plans
            SET plan_status  = ?,
                operator_note = ?,
                {ts_field}   = ?
            WHERE plan_id = ?
        """, (new_status, note or None, now, plan_id))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "plan_id": plan_id, "status": new_status,
            ts_field: now}


def approve_execution_plan(
    plan_id: str,
    note:    str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """
    Mark a plan as approved. Records operator intent only.
    Does NOT trigger execution.

    Returns:
        {"ok": True, "plan_id": ..., "status": "approved"}
        {"ok": False, "code": "not_found"}
    """
    return _transition_plan(plan_id, "approved", note, "approved_at",
                            _resolve_db(db_path))


def reject_execution_plan(
    plan_id: str,
    note:    str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """
    Mark a plan as rejected. Records operator intent only.

    Returns:
        {"ok": True, "plan_id": ..., "status": "rejected"}
        {"ok": False, "code": "not_found"}
    """
    return _transition_plan(plan_id, "rejected", note, "rejected_at",
                            _resolve_db(db_path))


def cancel_execution_plan(
    plan_id: str,
    note:    str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """
    Cancel a plan. Records operator intent only.

    Returns:
        {"ok": True, "plan_id": ..., "status": "cancelled"}
        {"ok": False, "code": "not_found"}
    """
    db = _resolve_db(db_path)
    initialize_schema(db)
    plan = get_execution_plan(plan_id, db_path=db)
    if plan is None:
        return {"ok": False, "code": "not_found",
                "error": f"Plan {plan_id!r} not found."}

    now = _now_iso()
    conn = get_connection(db)
    try:
        conn.execute("""
            UPDATE decision_execution_plans
            SET plan_status   = 'cancelled',
                operator_note = ?
            WHERE plan_id = ?
        """, (note or None, plan_id))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "plan_id": plan_id, "status": "cancelled",
            "cancelled_at": now}


def dry_run_execution_plan(
    plan_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Simulate execution of a plan — validates and returns steps without doing anything.

    Guarantees:
        - plan_status is NEVER set to 'executed'.
        - No episode is launched.
        - No publishing call is made.
        - No provider / API calls are made.
        - No network calls.

    Returns:
        {"ok": True, "plan_id": ..., "dry_run": True, "simulated_steps": [...],
         "validation": {...}, "warnings": [...]}
        {"ok": False, "code": "not_found" | "live_mode_blocked"}
    """
    db = _resolve_db(db_path)
    initialize_schema(db)

    plan = get_execution_plan(plan_id, db_path=db)
    if plan is None:
        return {"ok": False, "code": "not_found",
                "error": f"Plan {plan_id!r} not found."}

    if plan.get("execution_mode") == "live":
        return {"ok": False, "code": "live_mode_blocked",
                "error": "Live execution mode is blocked in F5. Only dry_run is permitted."}

    steps     = plan.get("planned_steps") or []
    blockers  = plan.get("blockers") or []
    risk      = plan.get("risk_level", "blocked")
    is_blocked = risk == "blocked" or bool(blockers)

    simulated = [
        {"step": i + 1, "description": s, "simulated": True, "executed": False}
        for i, s in enumerate(steps)
    ]

    validation = {
        "can_proceed":  not is_blocked,
        "risk_level":   risk,
        "blockers":     blockers,
        "step_count":   len(steps),
        "execution_mode": plan["execution_mode"],
        "f5_guard":     "executed status unreachable — dry_run only",
    }

    warnings = ["F5 guard active: plan_status will not advance to 'executed'."]
    if is_blocked:
        warnings.append(f"Plan is blocked: {'; '.join(blockers)}")

    return {
        "ok":              True,
        "plan_id":         plan_id,
        "dry_run":         True,
        "simulated_steps": simulated,
        "validation":      validation,
        "warnings":        warnings,
    }
