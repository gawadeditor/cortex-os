#!/usr/bin/env python3
"""
scripts/approve_run.py — P3-04: Approval CLI

Approve or reject a pending approval request from the command line.

Usage:
    python3 scripts/approve_run.py --approval-id APPROVAL_ID --user-id USER_ID
    python3 scripts/approve_run.py --approval-id APPROVAL_ID --user-id USER_ID --reject
    python3 scripts/approve_run.py --approval-id APPROVAL_ID --user-id USER_ID --notes "Looks good"
    python3 scripts/approve_run.py --list-pending
    python3 scripts/approve_run.py --list-pending --run-id RUN_ID

Exit codes:
    0 — approved / rejected successfully
    1 — approval not found, permission denied, already decided, or error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.approvals import (
    ApprovalAlreadyDecided,
    ApprovalNotFoundError,
    PermissionDeniedError,
    approve,
    get_approval,
    list_pending_approvals,
    reject,
)


def _print_record(record) -> None:
    print(f"  approval_id:   {record.approval_id}")
    print(f"  run_id:        {record.run_id}")
    print(f"  episode_id:    {record.episode_id}")
    print(f"  type:          {record.approval_type}")
    print(f"  requested_by:  {record.requested_by}")
    print(f"  requested_at:  {record.requested_at}")
    print(f"  status:        {record.status}")
    if record.decided_by:
        print(f"  decided_by:    {record.decided_by}")
        print(f"  decided_at:    {record.decided_at}")
    if record.notes:
        print(f"  notes:         {record.notes}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Approve or reject a Factory OS approval request.",
    )
    parser.add_argument("--approval-id", help="Approval UUID to decide on.")
    parser.add_argument("--user-id", help="User ID of the deciding operator.")
    parser.add_argument(
        "--reject", action="store_true",
        help="Reject the approval request (default: approve).",
    )
    parser.add_argument("--notes", default=None, help="Optional notes to store.")
    parser.add_argument("--list-pending", action="store_true",
                        help="List all pending approvals.")
    parser.add_argument("--run-id", default=None,
                        help="Filter --list-pending to a specific run.")
    parser.add_argument("--show", help="Show details for a specific approval_id.")
    args = parser.parse_args(argv)

    # ── List pending ──────────────────────────────────────────────────────
    if args.list_pending:
        pending = list_pending_approvals(run_id=args.run_id)
        if not pending:
            print("No pending approvals.")
            return 0
        print(f"{len(pending)} pending approval(s):\n")
        for rec in pending:
            _print_record(rec)
            print()
        return 0

    # ── Show single record ────────────────────────────────────────────────
    if args.show:
        record = get_approval(args.show)
        if record is None:
            print(f"Approval {args.show!r} not found.", file=sys.stderr)
            return 1
        _print_record(record)
        return 0

    # ── Approve / Reject ──────────────────────────────────────────────────
    if not args.approval_id:
        parser.error("--approval-id is required to approve or reject.")
    if not args.user_id:
        parser.error("--user-id is required to approve or reject.")

    action = "reject" if args.reject else "approve"
    try:
        if args.reject:
            reject(args.approval_id, args.user_id, notes=args.notes)
            print(f"Approval {args.approval_id!r} rejected by {args.user_id!r}.")
        else:
            approve(args.approval_id, args.user_id, notes=args.notes)
            print(f"Approval {args.approval_id!r} approved by {args.user_id!r}.")
            print("Run will advance to awaiting_publish_approval.")
        return 0

    except PermissionDeniedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ApprovalNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ApprovalAlreadyDecided as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
