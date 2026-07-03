"""
hypogeum/runtime.py — Local-Safe Autonomous Runtime Smoke

Wires the complete Factory OS execution path end-to-end without calling
run_pipeline or generating any content:

    schedule tick
        → scheduled_runs (pending)
        → consume_scheduled_runs (run_queue queued)
        → poll(dry_run=True)
        → run_queue completed

Use smoke_run() to verify the full scheduler → queue → consumer path in any
environment (local, CI, staging). It is always safe: dry_run=True is enforced
inside smoke_run() regardless of arguments.

Public API:
    smoke_run(db_path=None, worker_id=None) -> SmokeResult
    SmokeResult                              — dataclass with success + diagnostics

Steps executed:
    1. schedule_created     — create_schedule(factory scope, manual cadence)
    2. tick_fired           — tick() fires the schedule, producing a scheduled_run
    3. scheduled_run_queued — consume_scheduled_runs() promotes to run_queue
    4. poll_completed       — poll(dry_run=True) claims and completes the item
    5. db_state_verified    — DB confirms run_queue row has status='completed'
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.queue import PollResult, consume_scheduled_runs, get_item, poll
from hypogeum.scheduler import create_schedule, tick

# Hard cap for the smoke poll pass. Prevents infinite loops if poll() is called
# with more queue items than expected (e.g., leftover items from prior runs in
# the same DB). Smoke creates exactly one item, so this bound is never hit in
# normal operation.
_SMOKE_MAX_ITERATIONS = 10


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SmokeResult:
    """
    Outcome of a smoke_run() invocation.

    success:           True if all 5 steps completed without error.
    schedule_id:       ID of the schedule created by smoke_run().
    scheduled_run_ids: IDs created by tick().
    queue_ids:         IDs created by consume_scheduled_runs().
    poll_result:       Full PollResult from the dry_run poll pass.
    steps_completed:   Ordered list of step names that completed before stop.
    failure_reason:    Human-readable explanation when success=False.
    """
    success: bool = False
    schedule_id: Optional[str] = None
    scheduled_run_ids: List[str] = field(default_factory=list)
    queue_ids: List[str] = field(default_factory=list)
    poll_result: Optional[PollResult] = None
    steps_completed: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None

    def __str__(self) -> str:
        status = "OK" if self.success else "FAIL"
        steps = " -> ".join(self.steps_completed) if self.steps_completed else "(none)"
        lines = [
            f"SmokeResult({status})",
            f"  steps:     {steps}",
            f"  schedule:  {self.schedule_id}",
        ]
        if self.scheduled_run_ids:
            lines.append(f"  sched_runs: {self.scheduled_run_ids}")
        if self.queue_ids:
            lines.append(f"  queue_ids: {self.queue_ids}")
        if self.poll_result:
            pr = self.poll_result
            lines.append(
                f"  poll:      completed={pr.completed} failed={pr.failed} "
                f"stop={pr.stop_reason!r}"
            )
        if self.failure_reason:
            lines.append(f"  REASON:    {self.failure_reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Smoke run
# ---------------------------------------------------------------------------

def smoke_run(
    db_path: Optional[Path] = None,
    worker_id: Optional[str] = None,
) -> SmokeResult:
    """
    Execute a local-safe end-to-end smoke test of the Factory OS execution path.

    Wires: schedule → scheduled_run → run_queue → poll(dry_run=True)

    Guarantees:
        - dry_run=True is always enforced; run_pipeline is never called.
        - No content is generated.
        - No publishing occurs.
        - No provider API calls are made.
        - Safe to run in any environment.

    A factory-scoped, manual-cadence schedule is created with next_run_at in
    the past so tick() fires immediately. After the tick, consume_scheduled_runs()
    promotes the pending row to run_queue, and poll(dry_run=True) claims and
    completes the item. The final DB state is verified before returning.

    Args:
        db_path:   Override database path (for tests).
        worker_id: Worker identifier for the poll pass. Defaults to a new UUID4.

    Returns:
        SmokeResult with success=True if all 5 steps complete, or success=False
        with a failure_reason describing which step failed and why.
    """
    if worker_id is None:
        worker_id = str(uuid.uuid4())

    initialize_schema(db_path)
    result = SmokeResult()

    try:
        # ---------------------------------------------------------------
        # Step 0: Ensure the channel is in 'active' state so that
        #         poll()'s CALOG check does not skip the queue item.
        # ---------------------------------------------------------------
        from hypogeum.calog import get_channel_state, set_channel_state
        if get_channel_state("hypogeum", db_path=db_path) != "active":
            set_channel_state(
                "hypogeum", "active",
                triggered_by="smoke_run",
                db_path=db_path,
            )

        # ---------------------------------------------------------------
        # Step 1: Create a factory-scoped manual schedule with a past
        #         next_run_at so tick() fires it immediately.
        # ---------------------------------------------------------------
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        schedule_id = create_schedule(
            db_path=db_path,
            scope="factory",
            channel_id="hypogeum",
            cadence="manual",
            next_run_at=past,
            label="smoke_test",
        )
        result.schedule_id = schedule_id
        result.steps_completed.append("schedule_created")

        # ---------------------------------------------------------------
        # Step 2: Tick the scheduler.
        #         tick() advances next_run_at; for manual cadence it
        #         clears it to None (one-shot).
        # ---------------------------------------------------------------
        sr_ids = tick(db_path=db_path, now=datetime.now(timezone.utc))
        if not sr_ids:
            result.failure_reason = (
                "tick() produced no scheduled_runs. "
                "Check that the schedule next_run_at was in the past."
            )
            return result
        result.scheduled_run_ids = sr_ids
        result.steps_completed.append("tick_fired")

        # ---------------------------------------------------------------
        # Step 3: Promote pending scheduled_runs → run_queue (queued).
        # ---------------------------------------------------------------
        queue_ids = consume_scheduled_runs(db_path=db_path)
        if not queue_ids:
            result.failure_reason = (
                "consume_scheduled_runs() produced no run_queue entries. "
                f"scheduled_run_ids={sr_ids}"
            )
            return result
        result.queue_ids = queue_ids
        result.steps_completed.append("scheduled_run_queued")

        # ---------------------------------------------------------------
        # Step 4: Poll with dry_run=True.
        #         channel_id is None (factory scope) so CALOG is skipped.
        #         dry_run=True guarantees pipeline is never called.
        # ---------------------------------------------------------------
        poll_result = poll(
            worker_id=worker_id,
            db_path=db_path,
            dry_run=True,
            max_iterations=_SMOKE_MAX_ITERATIONS,
            max_stall_iterations=3,
            sleep_interval=0.0,
        )
        result.poll_result = poll_result

        if poll_result.completed == 0:
            result.failure_reason = (
                f"poll() completed 0 items "
                f"(stop_reason={poll_result.stop_reason!r}, "
                f"failed={poll_result.failed}, skipped={poll_result.skipped})"
            )
            return result
        result.steps_completed.append("poll_completed")

        # ---------------------------------------------------------------
        # Step 5: Verify DB state — queue item must be completed.
        # ---------------------------------------------------------------
        conn = get_connection(db_path)
        try:
            completed_count = conn.execute(
                "SELECT COUNT(*) FROM run_queue WHERE status = 'completed'"
            ).fetchone()[0]
        finally:
            conn.close()

        if completed_count == 0:
            result.failure_reason = (
                "poll() reported completed items but run_queue shows 0 completed rows."
            )
            return result
        result.steps_completed.append("db_state_verified")

        result.success = True
        return result

    except Exception as exc:
        result.failure_reason = f"Unexpected error at step {result.steps_completed[-1] if result.steps_completed else '(init)'}: {exc}"
        return result
