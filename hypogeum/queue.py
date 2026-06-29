"""
hypogeum/queue.py — Run Queue and Queue Claiming (P2-04) + Queue Consumer

Implements a durable, local-safe work queue between the scheduler and the
pipeline. Workers claim items atomically; the queue processor checks CALOG
and the topic backlog before calling run_pipeline().

Architecture:
    Scheduler (P2-03) creates scheduled_runs rows (status='pending').
    Queue consumer reads pending scheduled_runs and promotes them to run_queue
    entries (status='queued'), marking the scheduled_run as 'triggered'.
    Workers call claim_next() to atomically acquire one item.
    Workers call complete() or fail() when done.

Local-safe design:
  - No publishing.
  - No content generation outside run_pipeline().
  - run_pipeline() is only called when dry_run=False AND CALOG passes AND a
    real topic is available from the topic backlog.

Queue lifecycle:
    queued   → claimed    (worker atomically claims via claim_next)
    queued   → cancelled  (operator cancels before claim)
    claimed  → completed  (worker reports success)
    claimed  → failed     (worker reports failure)
    claimed  → queued     (release — worker requeues on crash or timeout)

Queue Consumer process_next() flow:
    1. Claim queue item atomically.
    2. CALOG check: release or fail based on channel state.
    3. dry_run=True: complete immediately, no pipeline.
    4. Topic resolution: claim next topic from backlog via topics.py.
       - No topic available: release queue item, return status="no_topic".
    5. Call run_pipeline() with real topic_text.
    6. On success: consume_topic(), complete().
    7. On failure: fail() (topic stays claimed — prevents infinite retry).

ProcessResult.status values:
    "completed" — pipeline ran (or dry_run) successfully
    "failed"    — pipeline raised an exception
    "skipped"   — CALOG denied; item released or failed based on channel state
    "empty"     — queue was empty; nothing claimed
    "no_topic"  — channel has no queued topics; queue item released for retry

Queue Poller:
    poll() wraps process_next() in a loop. Stops when the queue is empty,
    when max_iterations is reached, or on KeyboardInterrupt. Always returns
    a PollResult with per-status counts and a stop_reason.

Public API:
    enqueue(...)                -> str            (queue_id)
    claim_next(...)             -> dict | None
    complete(...)               -> None
    fail(...)                   -> None
    cancel(...)                 -> None
    release(...)                -> None
    consume_scheduled_runs(...) -> list[str]      (queue_ids created)
    get_queue_stats(...)        -> dict
    process_next(...)           -> ProcessResult  (Queue Consumer entry point)
    poll(...)                   -> PollResult     (Queue Poller entry point)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import hypogeum.pipeline as _pipeline_module  # patched by tests via monkeypatch

from hypogeum.db import (
    abort_queue_item,
    cancel_queue_item,
    claim_next_queue_item,
    complete_queue_item,
    fail_queue_item,
    get_connection,
    get_pending_scheduled_runs,
    get_queue_item,
    initialize_schema,
    insert_queue_item,
    is_cancel_requested,
    release_queue_item,
    set_worker_control,
    update_scheduled_run_status,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def enqueue(
    db_path: Optional[Path] = None,
    scheduled_run_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    priority: int = 0,
    source: Optional[str] = None,
    recovery_from_run_id: Optional[str] = None,
) -> str:
    """
    Add a work item to the run queue.

    Items can be enqueued directly (e.g., from a manual trigger) or
    via consume_scheduled_runs() which promotes pending scheduled_runs.
    Does not execute the pipeline or generate any content.

    Args:
        db_path:               Override DB path (for tests).
        scheduled_run_id:      FK to scheduled_runs row (optional).
        schedule_id:           Denormalized schedule FK (optional).
        channel_id:            Channel scope (optional).
        episode_id:            Links queue item to a specific episode for reruns.
        priority:              Claim ordering — lower integer = higher priority.
                               Default 0. Use negative values for urgent items.
        source:                'rerun' | 'schedule' | 'manual'. Used for claim
                               ordering (reruns are preferred over schedules).
        recovery_from_run_id:  run_id of the failed run this item recovers.
                               Set for source='rerun' items.

    Returns:
        New queue_id (UUID4 string).
    """
    initialize_schema(db_path)
    queue_id = _new_id()
    now = _iso(_now_utc())

    conn = get_connection(db_path)
    try:
        insert_queue_item(
            conn,
            queue_id=queue_id,
            queued_at=now,
            created_at=now,
            scheduled_run_id=scheduled_run_id,
            schedule_id=schedule_id,
            channel_id=channel_id,
            episode_id=episode_id,
            priority=priority,
            source=source,
            recovery_from_run_id=recovery_from_run_id,
        )
        conn.commit()
    finally:
        conn.close()

    return queue_id


def claim_next(
    worker_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Atomically claim the next highest-priority queued item.

    Uses a conditional UPDATE to prevent double-claiming: the UPDATE's
    WHERE clause requires status='queued' on the specific candidate row,
    so only one concurrent caller succeeds per row.

    Never executes pipeline logic. Only transitions status queued→claimed.

    Args:
        worker_id: Unique identifier for this worker (e.g., UUID4 per process).
        db_path:   Override DB path (for tests).

    Returns:
        Dict of the claimed run_queue row, or None if the queue is empty
        or the candidate was claimed by another worker first.
    """
    conn = get_connection(db_path)
    try:
        claimed = claim_next_queue_item(conn, worker_id, _iso(_now_utc()))
        conn.commit()
        return claimed
    finally:
        conn.close()


def complete(
    queue_id: str,
    worker_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Mark a claimed item as completed.

    Only succeeds if the item is currently 'claimed' by worker_id.
    Items in any other status are unchanged (no-op, no error raised).

    Never executes pipeline logic.

    Args:
        queue_id:  Target queue item.
        worker_id: Must match the worker that claimed the item.
        db_path:   Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        complete_queue_item(conn, queue_id, worker_id, _iso(_now_utc()))
        conn.commit()
    finally:
        conn.close()


def fail(
    queue_id: str,
    worker_id: str,
    error_message: str = "",
    db_path: Optional[Path] = None,
) -> None:
    """
    Mark a claimed item as failed.

    Only succeeds if the item is currently 'claimed' by worker_id.

    Args:
        queue_id:      Target queue item.
        worker_id:     Must match the worker that claimed the item.
        error_message: Description of the failure (truncated at 4096 chars).
        db_path:       Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        fail_queue_item(conn, queue_id, worker_id, _iso(_now_utc()), error_message)
        conn.commit()
    finally:
        conn.close()


def cancel(
    queue_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Cancel a queued item before it is claimed.

    Only 'queued' items can be cancelled. Already-claimed or terminal items
    are unchanged (no-op, no error raised).

    Args:
        queue_id: Target queue item.
        db_path:  Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        cancel_queue_item(conn, queue_id)
        conn.commit()
    finally:
        conn.close()


def release(
    queue_id: str,
    worker_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Release a claimed item back to 'queued' for re-claiming.

    Used when a worker crashes, times out, or detects it cannot process
    the item. Only succeeds for items currently 'claimed' by worker_id.

    Args:
        queue_id:  Target queue item.
        worker_id: Must match the worker that holds the claim.
        db_path:   Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        release_queue_item(conn, queue_id, worker_id)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

def consume_scheduled_runs(
    db_path: Optional[Path] = None,
) -> list[str]:
    """
    Promote all pending scheduled_runs to run_queue entries.

    For each scheduled_run with status='pending':
      1. Insert a run_queue row with status='queued'.
      2. Update the scheduled_run status to 'triggered'.

    Does not execute the pipeline. Does not generate content.
    Does not publish. Pure state promotion.

    Args:
        db_path: Override DB path (for tests).

    Returns:
        List of queue_ids created (one per promoted scheduled_run).
    """
    now = _iso(_now_utc())
    created_ids: list[str] = []

    conn = get_connection(db_path)
    try:
        pending = get_pending_scheduled_runs(conn)
        for row in pending:
            queue_id = _new_id()
            insert_queue_item(
                conn,
                queue_id=queue_id,
                queued_at=now,
                created_at=now,
                scheduled_run_id=row["scheduled_run_id"],
                schedule_id=row["schedule_id"],
                channel_id=row["channel_id"],
                episode_id=row["episode_id"],
                priority=0,
            )
            update_scheduled_run_status(
                conn,
                scheduled_run_id=row["scheduled_run_id"],
                status="triggered",
                triggered_at=now,
            )
            created_ids.append(queue_id)
        conn.commit()
    finally:
        conn.close()

    return created_ids


# ---------------------------------------------------------------------------
# Queue queries
# ---------------------------------------------------------------------------

def get_queue_stats(db_path: Optional[Path] = None) -> dict:
    """
    Return counts of queue items by status.

    Args:
        db_path: Override DB path (for tests).

    Returns:
        Dict with keys: queued, claimed, completed, failed, cancelled, total.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as cnt
            FROM run_queue
            GROUP BY status
            """
        ).fetchall()
    finally:
        conn.close()

    counts = {s: 0 for s in ("queued", "claimed", "completed", "failed", "cancelled")}
    for row in rows:
        counts[row["status"]] = row["cnt"]
    counts["total"] = sum(counts.values())
    return counts


def get_item(
    queue_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return a single run_queue row as a dict, or None if not found."""
    conn = get_connection(db_path)
    try:
        return get_queue_item(conn, queue_id)
    finally:
        conn.close()


# ===========================================================================
# Queue Consumer
# ===========================================================================

# States that are permanently non-runnable — fail item instead of releasing
_PERMANENT_BLOCK_STATES = frozenset({"archived", "error"})

# Guard string — must never reach run_pipeline as topic_input.
# If it does, something bypassed the topic backlog check.
_PLACEHOLDER_TOPIC = "pending_topic_assignment"


@dataclass
class ProcessResult:
    """
    Result of one process_next() invocation.

    status values:
        "completed" — pipeline ran (or dry_run) successfully; item done
        "failed"    — pipeline raised an exception; item marked failed
        "skipped"   — CALOG denied the run; item released or failed
        "empty"     — queue was empty; nothing claimed
        "no_topic"  — channel has no queued topics; queue item released
        "cancelled" — operator cancelled the run; item marked cancelled
    """
    status: str
    queue_id: Optional[str]
    channel_id: Optional[str]
    worker_id: str
    dry_run: bool = True
    error_message: Optional[str] = None
    topic_id: Optional[str] = None


def process_next(
    worker_id: str,
    db_path: Optional[Path] = None,
    dry_run: bool = True,
    config=None,
    topic_resolver: Optional[Callable[[dict], Optional[str]]] = None,
) -> ProcessResult:
    """
    Claim the next queued item, check CALOG, resolve a topic, run pipeline.

    Flow:
        1. Atomically claim the highest-priority queued item.
           → If empty: return ProcessResult(status="empty").
        2. CALOG check on claimed item's channel_id.
           → Permanently blocked (archived/error): fail item, return "skipped".
           → Temporarily blocked (paused/blocked/draft): release, return "skipped".
        3. dry_run=True: complete item immediately, return "completed". No pipeline.
        4. Topic resolution (dry_run=False only):
           a. If topic_resolver is provided: call it with the claimed item dict.
              Must return a non-empty string or None.
           b. Otherwise: call topics.claim_next_topic(channel_id) from backlog.
              topic_id is kept for the consume step on success.
           c. If no topic (None or empty): release queue item, return "no_topic".
           d. If topic is the placeholder sentinel: block — never reaches pipeline.
        5. Call run_pipeline(topic_input, episode_id, config, channel_id).
           → On success: consume_topic (if backlog path), complete item, return "completed".
           → On exception: fail item, return "failed".
           Topic stays 'claimed' on failure — prevents infinite retry loop.

    Worker ownership:
        worker_id is stored on the run_queue row at claim time. complete() and
        fail() both enforce AND worker_id = ?, so only the claiming worker closes
        the item.

    Args:
        worker_id:       UUID4 per worker process. Must be unique and stable
                         for the lifetime of a single consumer pass.
        db_path:         Override DB path (for tests).
        dry_run:         If True: CALOG is checked but pipeline is never called.
                         Item is marked completed immediately. Useful for local
                         testing and scheduler smoke-tests.
        config:          Config object for run_pipeline. If None and dry_run=False,
                         Config() is loaded from environment.
        topic_resolver:  Optional Callable[[dict], Optional[str]]. Receives the
                         claimed run_queue row dict; returns topic_text or None.
                         When None, the default backlog path is used (recommended).
                         Custom resolvers are responsible for their own topic
                         lifecycle (consume_topic is NOT called for custom resolvers).

    Failure hygiene (dry_run=False, backlog path):
        On any pipeline exception:
          - queue item is marked 'failed'.
          - run_registry row is marked 'failed' by run_pipeline itself.
          - PipelineRejectedError: topic marked 'skipped' (EP rejection is permanent).
          - All other exceptions: topic released back to 'queued' for retry.

    Returns:
        ProcessResult with status one of:
            "completed" — success (pipeline ran or dry_run)
            "failed"    — pipeline raised an exception
            "skipped"   — CALOG denied; item released or failed
            "empty"     — queue was empty
            "no_topic"  — no queued topic; queue item released for retry
    """
    # Step 1: Claim atomically
    claimed = claim_next(worker_id=worker_id, db_path=db_path)
    if claimed is None:
        return ProcessResult(
            status="empty",
            queue_id=None,
            channel_id=None,
            worker_id=worker_id,
            dry_run=dry_run,
        )

    queue_id = claimed["queue_id"]
    channel_id = claimed.get("channel_id")

    # Step 2: CALOG check — only when a channel is scoped to this item
    if channel_id is not None:
        from hypogeum.calog import get_channel_state, is_channel_runnable

        if not is_channel_runnable(channel_id, db_path=db_path):
            state = get_channel_state(channel_id, db_path=db_path)

            if state in _PERMANENT_BLOCK_STATES:
                fail(
                    queue_id, worker_id,
                    error_message=(
                        f"Channel '{channel_id}' is in state '{state}'; "
                        f"item cannot be processed and will not be retried."
                    ),
                    db_path=db_path,
                )
            else:
                release(queue_id, worker_id, db_path=db_path)

            return ProcessResult(
                status="skipped",
                queue_id=queue_id,
                channel_id=channel_id,
                worker_id=worker_id,
                dry_run=dry_run,
                error_message=(
                    f"Channel '{channel_id}' is not runnable (state: '{state}')."
                ),
            )

    # Step 3: dry_run — complete without topic resolution or pipeline
    if dry_run:
        complete(queue_id, worker_id, db_path=db_path)
        return ProcessResult(
            status="completed",
            queue_id=queue_id,
            channel_id=channel_id,
            worker_id=worker_id,
            dry_run=True,
        )

    # Step 4: Resolve topic — required for dry_run=False
    topic_text: Optional[str] = None
    topic_id: Optional[str] = None
    using_backlog = False

    if topic_resolver is not None:
        # Caller-provided resolver — caller owns topic lifecycle
        resolved = topic_resolver(claimed)
        if resolved and resolved != _PLACEHOLDER_TOPIC:
            topic_text = resolved
        # else: falls through to no_topic handling below
    elif channel_id is not None:
        # Default path: claim from backlog
        from hypogeum.topics import claim_next_topic
        topic_row = claim_next_topic(channel_id, db_path=db_path)
        if topic_row is not None:
            topic_text = topic_row["topic_text"]
            topic_id = topic_row["topic_id"]
            using_backlog = True

    if not topic_text or topic_text == _PLACEHOLDER_TOPIC:
        # No topic available — release queue item for retry on next pass
        release(queue_id, worker_id, db_path=db_path)
        return ProcessResult(
            status="no_topic",
            queue_id=queue_id,
            channel_id=channel_id,
            worker_id=worker_id,
            dry_run=False,
            error_message=(
                f"No queued topic available for channel '{channel_id}'. "
                f"Queue item released for retry."
            ),
        )

    # Step 5: Execute pipeline
    try:
        if config is None:
            from hypogeum.config import Config
            config = Config()

        # Prefer episode_id from the topic row (set for reruns to reuse disk checkpoints),
        # fall back to the queue row's episode_id, then to a hash of the queue_id.
        _topic_ep_id = (topic_row.get("episode_id") if using_backlog and topic_row else None)
        episode_id = _topic_ep_id or claimed.get("episode_id") or f"ep_{queue_id[:8]}"

        # Cancellation check: polled by pipeline.py between every stage.
        # Also updates worker_status to 'cancelling' on first detection.
        def _cancel_check() -> bool:
            cancelled = is_cancel_requested(queue_id, db_path=db_path)
            if cancelled:
                try:
                    set_worker_control("worker_status", "cancelling", db_path=db_path)
                except Exception:
                    pass
            return cancelled

        # Write running status before pipeline starts
        try:
            set_worker_control("worker_status", "running", db_path=db_path)
            set_worker_control("active_queue_id", queue_id, db_path=db_path)
        except Exception:
            pass

        _pipeline_module.run_pipeline(
            topic_input=topic_text,
            episode_id=episode_id,
            config=config,
            channel_id=channel_id,
            cancel_check=_cancel_check,
        )

        # Mark topic consumed on success (backlog path only)
        if using_backlog and topic_id is not None:
            from hypogeum.topics import consume_topic
            consume_topic(topic_id, db_path=db_path)

        complete(queue_id, worker_id, db_path=db_path)
        try:
            set_worker_control("worker_status", "idle", db_path=db_path)
            set_worker_control("active_queue_id", "", db_path=db_path)
        except Exception:
            pass
        return ProcessResult(
            status="completed",
            queue_id=queue_id,
            channel_id=channel_id,
            worker_id=worker_id,
            dry_run=False,
            topic_id=topic_id,
        )

    except _pipeline_module.PipelineCancelledError as exc:
        # Operator cancelled — force-cancel the queue item and release the topic.
        abort_queue_item(queue_id, db_path=db_path)
        if topic_id is not None and using_backlog:
            try:
                from hypogeum.topics import release_topic
                release_topic(topic_id, db_path=db_path)
            except Exception:
                pass
        try:
            set_worker_control("worker_status", "idle", db_path=db_path)
            set_worker_control("active_queue_id", "", db_path=db_path)
        except Exception:
            pass
        return ProcessResult(
            status="cancelled",
            queue_id=queue_id,
            channel_id=channel_id,
            worker_id=worker_id,
            dry_run=False,
            error_message="cancelled by operator request",
            topic_id=topic_id,
        )

    except Exception as exc:
        error_msg = str(exc)[:4096]
        fail(queue_id, worker_id, error_message=error_msg, db_path=db_path)

        # Topic hygiene: release or skip the topic based on failure type.
        # PipelineRejectedError means the EP scored the topic below threshold —
        # the topic content itself is the problem, so skip it permanently.
        # All other exceptions are infrastructure failures — release for retry.
        if topic_id is not None and using_backlog:
            try:
                if isinstance(exc, _pipeline_module.PipelineRejectedError):
                    from hypogeum.topics import skip_topic
                    skip_topic(topic_id, reason="ep_rejected", db_path=db_path)
                else:
                    from hypogeum.topics import release_topic
                    release_topic(topic_id, db_path=db_path)
            except Exception:
                pass  # never let cleanup block the result

        try:
            set_worker_control("worker_status", "idle", db_path=db_path)
            set_worker_control("active_queue_id", "", db_path=db_path)
        except Exception:
            pass
        return ProcessResult(
            status="failed",
            queue_id=queue_id,
            channel_id=channel_id,
            worker_id=worker_id,
            dry_run=False,
            error_message=error_msg,
            topic_id=topic_id,
        )


# ===========================================================================
# Queue Poller
# ===========================================================================

@dataclass
class PollResult:
    """
    Summary of a poll() run.

    stop_reason values:
        "empty"          — queue had no more items; poller stopped naturally.
        "max_iterations" — iteration limit reached before queue was exhausted.
        "stalled"        — max_stall_iterations consecutive non-progress results
                           (no_topic or skipped) triggered the stall guard.
        "interrupted"    — KeyboardInterrupt received; loop exited cleanly.

    Counts reflect every process_next() call made during the poll run.
    """
    iterations: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    no_topic: int = 0
    cancelled: int = 0
    stop_reason: str = "empty"
    stall_streak: int = 0
    results: List[ProcessResult] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.completed + self.failed + self.skipped + self.no_topic + self.cancelled


def poll(
    worker_id: str,
    db_path: Optional[Path] = None,
    dry_run: bool = True,
    max_iterations: Optional[int] = None,
    sleep_interval: float = 0.0,
    config=None,
    topic_resolver: Optional[Callable[[dict], Optional[str]]] = None,
    max_stall_iterations: int = 100,
) -> PollResult:
    """
    Run process_next() in a loop until the queue is empty, the iteration
    limit is reached, or the process receives a KeyboardInterrupt.

    The poller is the local runtime bridge between the topic backlog and the
    pipeline. It consumes queued items one at a time, checking CALOG and the
    topic backlog before each pipeline call.

    Stop conditions (checked in order after each iteration):
        1. result.status == "empty": no more items in queue → stop cleanly.
        2. max_stall_iterations consecutive non-progress results (no_topic or
           skipped) → stop with stop_reason="stalled". Protects against infinite
           loops when released items keep cycling back through the queue.
        3. max_iterations reached (if set) → stop after processing that item.
        4. KeyboardInterrupt → stop immediately after the current item finishes.

    When dry_run=True:
        - process_next() marks each claimed item completed without calling
          run_pipeline(). Topics are not claimed from the backlog.
        - Use for local smoke-testing the scheduler→queue path end-to-end.

    When dry_run=False:
        - process_next() claims a topic from the backlog and calls run_pipeline().
        - Each successful pipeline run marks the topic consumed.
        - A failed run marks the queue item failed; the topic stays claimed.

    sleep_interval:
        Seconds to sleep between iterations. Default 0.0 (no sleep). Set to
        a positive value in production to avoid busy-looping when items trickle in.
        In tests always use 0.0 or monkeypatch time.sleep.

    Args:
        worker_id:             Unique identifier for this worker. Should be stable for
                               the lifetime of one poll() call (UUID4 recommended).
        db_path:               Override DB path (for tests).
        dry_run:               If True, never calls run_pipeline. Default True.
        max_iterations:        Stop after this many process_next() calls (excluding the
                               "empty" call that terminates the loop). None = no limit.
        sleep_interval:        Seconds to sleep between iterations. Default 0.0.
        config:                Config object for run_pipeline (dry_run=False only).
        topic_resolver:        Optional topic resolver callable. When None and
                               dry_run=False, the default backlog path is used.
        max_stall_iterations:  Stop with stop_reason="stalled" after this many
                               consecutive non-progress results (no_topic or skipped).
                               Prevents infinite loops when released items cycle back.
                               Default 100. Must be > 0.

    Returns:
        PollResult with per-status counts and stop_reason.
    """
    result = PollResult()
    stall_streak = 0

    try:
        while True:
            pr = process_next(
                worker_id=worker_id,
                db_path=db_path,
                dry_run=dry_run,
                config=config,
                topic_resolver=topic_resolver,
            )
            result.results.append(pr)

            if pr.status == "empty":
                result.stop_reason = "empty"
                break

            result.iterations += 1

            if pr.status == "completed":
                result.completed += 1
                stall_streak = 0
            elif pr.status == "failed":
                result.failed += 1
                stall_streak = 0
            elif pr.status == "cancelled":
                result.cancelled += 1
                stall_streak = 0
            elif pr.status == "skipped":
                result.skipped += 1
                stall_streak += 1
            elif pr.status == "no_topic":
                result.no_topic += 1
                stall_streak += 1

            result.stall_streak = stall_streak

            if stall_streak >= max_stall_iterations:
                result.stop_reason = "stalled"
                break

            if max_iterations is not None and result.iterations >= max_iterations:
                result.stop_reason = "max_iterations"
                break

            if sleep_interval > 0.0:
                time.sleep(sleep_interval)

    except KeyboardInterrupt:
        result.stop_reason = "interrupted"

    return result
