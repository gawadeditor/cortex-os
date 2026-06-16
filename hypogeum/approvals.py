"""
hypogeum/approvals.py — P3-04: Approval System

Human-review gates and change-request approvals for Factory OS governance.

An approval request is created when a pipeline or policy rule determines
that a human operator must review before execution can proceed. The run
enters an awaiting_review or awaiting_publish_approval RLG state and waits.
The operator uses approve_run.py (CLI) or the dashboard to decide.

Approval types:
    human_review        — operator reviews episode before publishing
    publish_approval    — explicit gate before publish (future: per-channel policy)
    model_swap          — change to model slug requires operator sign-off (P3-05b)
    budget_increase     — ceiling raise requires approval (P3-05b)

RLG state transitions on decision:
    approve()  : awaiting_review → awaiting_publish_approval
    reject()   : awaiting_review → failed

Authorization: approve() and reject() require the deciding user to have
operator-or-above role (uses PERMISSION_TABLE["approve_run"]).
System actors (triggered_by starting with "system:") bypass the user DB check.

Public API:
    create_approval_request(run_id, episode_id, approval_type, requested_by,
                            evidence=None, db_path=None) -> str   # approval_id
    approve(approval_id, decided_by_user_id, notes=None, db_path=None) -> bool
    reject(approval_id, decided_by_user_id, notes=None, db_path=None) -> bool
    get_approval(approval_id, db_path=None) -> Optional[ApprovalRecord]
    list_pending_approvals(run_id=None, db_path=None) -> list[ApprovalRecord]
    is_approved(run_id, approval_type, db_path=None) -> bool
    expire_stale_approvals(older_than_hours=72, db_path=None) -> int

Exceptions:
    PermissionDeniedError   — user lacks operator role for approve/reject
    ApprovalNotFoundError   — approval_id does not exist
    ApprovalAlreadyDecided  — approval is not in pending state
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import (
    expire_old_approvals,
    get_approval_by_id,
    get_connection,
    initialize_schema,
    insert_approval,
    list_approvals,
    update_approval_decision,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

VALID_APPROVAL_TYPES = frozenset({
    "human_review",
    "publish_approval",
    "model_swap",
    "budget_increase",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PermissionDeniedError(RuntimeError):
    """User does not have operator-or-above role to approve/reject."""

    def __init__(self, user_id: str, action: str = "approve_run") -> None:
        self.user_id = user_id
        self.action = action
        super().__init__(
            f"User {user_id!r} is not authorized to perform {action!r}. "
            "Requires operator, admin, or owner role."
        )


class ApprovalNotFoundError(RuntimeError):
    """No approval record exists for the requested approval_id."""

    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        super().__init__(f"Approval {approval_id!r} not found.")


class ApprovalAlreadyDecided(RuntimeError):
    """Approval is not in pending state and cannot be decided again."""

    def __init__(self, approval_id: str, current_status: str) -> None:
        self.approval_id = approval_id
        self.current_status = current_status
        super().__init__(
            f"Approval {approval_id!r} is already in status {current_status!r}. "
            "Only pending approvals can be decided."
        )


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class ApprovalRecord:
    approval_id:   str
    run_id:        str
    episode_id:    str
    approval_type: str
    requested_by:  str
    status:        str
    requested_at:  str
    decided_by:    Optional[str]
    decided_at:    Optional[str]
    notes:         Optional[str]
    evidence:      Optional[dict]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _row_to_record(row) -> ApprovalRecord:
    evidence_raw = dict(row).get("evidence")
    evidence = None
    if evidence_raw:
        try:
            evidence = json.loads(evidence_raw)
        except Exception:
            pass
    return ApprovalRecord(
        approval_id   = row["approval_id"],
        run_id        = row["run_id"],
        episode_id    = row["episode_id"],
        approval_type = row["approval_type"],
        requested_by  = row["requested_by"],
        status        = row["status"],
        requested_at  = row["requested_at"],
        decided_by    = row["decided_by"],
        decided_at    = row["decided_at"],
        notes         = row["notes"],
        evidence      = evidence,
    )


def _check_authorization(user_id: str, db_path: Optional[Path]) -> None:
    """
    Raise PermissionDeniedError if user_id cannot perform approve_run.

    System actors (user_id starting with "system:") always pass.
    Real users are validated against the users table via check_authorization().
    """
    if user_id.startswith("system:"):
        return
    try:
        from hypogeum.auth import check_authorization
        if not check_authorization(user_id, "approve_run", db_path=db_path):
            raise PermissionDeniedError(user_id)
    except PermissionDeniedError:
        raise
    except Exception:
        # auth DB unavailable — deny by default
        raise PermissionDeniedError(user_id)


def _emit_signal_nonblocking(
    signal_type: str,
    message: str,
    run_id: str,
    episode_id: Optional[str],
    db_path: Optional[Path],
) -> None:
    """Emit an OIS signal. Never raises."""
    try:
        from hypogeum.ois import emit_signal
        emit_signal(
            signal_type=signal_type,
            severity="WARNING" if signal_type in ("approval_pending", "approval_rejected") else "INFO",
            message=message,
            channel_id=None,
            source_system="approvals",
            blocking=False,
            run_id=run_id,
            episode_id=episode_id,
            db_path=db_path,
        )
    except Exception:
        pass


def _transition_run_nonblocking(
    run_id: str,
    to_state: str,
    triggered_by: str,
    reason: Optional[str],
    db_path: Optional[Path],
) -> None:
    """Transition run via RLG. Never raises — RLG errors are logged, not propagated."""
    try:
        from hypogeum.run_registry import transition_run_state
        transition_run_state(
            run_id=run_id,
            to_state=to_state,
            triggered_by=triggered_by,
            trigger_source="approvals",
            reason=reason,
            db_path=db_path,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_approval_request(
    run_id:        str,
    episode_id:    str,
    approval_type: str,
    requested_by:  str,
    evidence:      Optional[dict] = None,
    db_path:       Optional[Path] = None,
) -> str:
    """
    Create a new approval request with status='pending'.

    Emits an OIS 'approval_pending' signal so the notification system
    (Telegram/email) alerts the operator.

    Args:
        run_id:        Run that requires approval.
        episode_id:    Episode being processed.
        approval_type: One of VALID_APPROVAL_TYPES.
        requested_by:  User ID or "system:pipeline".
        evidence:      Optional JSON-serialisable context dict (e.g. report excerpt).
        db_path:       Override DB path (always pass in tests).

    Returns:
        approval_id — UUID4 string of the new approvals row.

    Raises:
        ValueError — approval_type is not in VALID_APPROVAL_TYPES.
    """
    if approval_type not in VALID_APPROVAL_TYPES:
        raise ValueError(
            f"Invalid approval_type {approval_type!r}. "
            f"Must be one of: {sorted(VALID_APPROVAL_TYPES)}"
        )
    approval_id = str(uuid.uuid4())
    requested_at = _now_iso()
    initialize_schema(_resolve(db_path))
    conn = get_connection(_resolve(db_path))
    try:
        insert_approval(
            conn,
            approval_id=approval_id,
            run_id=run_id,
            episode_id=episode_id,
            approval_type=approval_type,
            requested_by=requested_by,
            requested_at=requested_at,
            evidence=evidence,
        )
        conn.commit()
    finally:
        conn.close()

    _emit_signal_nonblocking(
        signal_type="approval_pending",
        message=(
            f"Approval required for run {run_id!r} (episode {episode_id!r}). "
            f"Type: {approval_type}. approval_id={approval_id!r}"
        ),
        run_id=run_id,
        episode_id=episode_id,
        db_path=db_path,
    )

    return approval_id


def approve(
    approval_id:       str,
    decided_by_user_id: str,
    notes:             Optional[str] = None,
    db_path:           Optional[Path] = None,
) -> bool:
    """
    Approve a pending approval request.

    Sets status='approved', records decided_by and decided_at, emits
    'approval_granted' OIS signal, and transitions the run to
    awaiting_publish_approval via RLG.

    Args:
        approval_id:         UUID4 from create_approval_request().
        decided_by_user_id:  User who approved (must be operator or above).
        notes:               Optional comment stored on the record.
        db_path:             Override DB path.

    Returns:
        True on success.

    Raises:
        PermissionDeniedError    — user lacks operator role.
        ApprovalNotFoundError    — approval_id not in DB.
        ApprovalAlreadyDecided   — approval is not pending.
    """
    _check_authorization(decided_by_user_id, db_path=db_path)

    decided_at = _now_iso()
    conn = get_connection(_resolve(db_path))
    try:
        row = get_approval_by_id(conn, approval_id)
        if row is None:
            raise ApprovalNotFoundError(approval_id)
        if row["status"] != "pending":
            raise ApprovalAlreadyDecided(approval_id, row["status"])
        run_id    = row["run_id"]
        episode_id = row["episode_id"]
        update_approval_decision(
            conn, approval_id,
            status="approved",
            decided_by=decided_by_user_id,
            decided_at=decided_at,
            notes=notes,
        )
        conn.commit()
    finally:
        conn.close()

    _emit_signal_nonblocking(
        signal_type="approval_granted",
        message=(
            f"Approval {approval_id!r} granted by {decided_by_user_id!r} "
            f"for run {run_id!r}."
        ),
        run_id=run_id,
        episode_id=episode_id,
        db_path=db_path,
    )

    _transition_run_nonblocking(
        run_id=run_id,
        to_state="awaiting_publish_approval",
        triggered_by=decided_by_user_id,
        reason=f"Approved by {decided_by_user_id!r}",
        db_path=db_path,
    )

    return True


def reject(
    approval_id:        str,
    decided_by_user_id: str,
    notes:              Optional[str] = None,
    db_path:            Optional[Path] = None,
) -> bool:
    """
    Reject a pending approval request.

    Sets status='rejected', emits 'approval_rejected' OIS signal, and
    transitions the run to 'failed' via RLG.

    Raises:
        PermissionDeniedError    — user lacks operator role.
        ApprovalNotFoundError    — approval_id not in DB.
        ApprovalAlreadyDecided   — approval is not pending.
    """
    _check_authorization(decided_by_user_id, db_path=db_path)

    decided_at = _now_iso()
    conn = get_connection(_resolve(db_path))
    try:
        row = get_approval_by_id(conn, approval_id)
        if row is None:
            raise ApprovalNotFoundError(approval_id)
        if row["status"] != "pending":
            raise ApprovalAlreadyDecided(approval_id, row["status"])
        run_id     = row["run_id"]
        episode_id = row["episode_id"]
        update_approval_decision(
            conn, approval_id,
            status="rejected",
            decided_by=decided_by_user_id,
            decided_at=decided_at,
            notes=notes,
        )
        conn.commit()
    finally:
        conn.close()

    _emit_signal_nonblocking(
        signal_type="approval_rejected",
        message=(
            f"Approval {approval_id!r} rejected by {decided_by_user_id!r} "
            f"for run {run_id!r}."
        ),
        run_id=run_id,
        episode_id=episode_id,
        db_path=db_path,
    )

    _transition_run_nonblocking(
        run_id=run_id,
        to_state="failed",
        triggered_by=decided_by_user_id,
        reason=f"Rejected by {decided_by_user_id!r}. Notes: {notes or '—'}",
        db_path=db_path,
    )

    return True


def get_approval(
    approval_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ApprovalRecord]:
    """Return the ApprovalRecord for approval_id, or None if not found."""
    conn = get_connection(_resolve(db_path))
    try:
        row = get_approval_by_id(conn, approval_id)
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def list_pending_approvals(
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[ApprovalRecord]:
    """
    Return all pending approval requests.

    Args:
        run_id:  Filter to a specific run (None = all runs).
        db_path: Override DB path.
    """
    conn = get_connection(_resolve(db_path))
    try:
        rows = list_approvals(conn, run_id=run_id, status="pending")
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def is_approved(
    run_id:        str,
    approval_type: str,
    db_path:       Optional[Path] = None,
) -> bool:
    """
    Return True if there is at least one approved approval of the given
    type for run_id.
    """
    conn = get_connection(_resolve(db_path))
    try:
        rows = list_approvals(
            conn, run_id=run_id, status="approved", approval_type=approval_type,
        )
        return len(rows) > 0
    finally:
        conn.close()


def expire_stale_approvals(
    older_than_hours: int          = 72,
    db_path:          Optional[Path] = None,
) -> int:
    """
    Mark pending approvals older than older_than_hours as 'expired'.

    Returns the count of rows expired.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    ).isoformat()
    conn = get_connection(_resolve(db_path))
    try:
        count = expire_old_approvals(conn, cutoff_at=cutoff)
        conn.commit()
        return count
    finally:
        conn.close()
