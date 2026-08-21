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
    provision_bounded_run(...)  -> dict            (explicit bounded-run provisioning)
    claim_next_bounded(...)     -> dict | None     (durable, capped claim — see below)
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

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

import hypogeum.pipeline as _pipeline_module  # patched by tests via monkeypatch

from hypogeum.db import (
    BoundedRunError,
    abort_queue_item,
    cancel_queue_item,
    claim_next_queue_item,
    complete_queue_item,
    count_bounded_run_claims,
    fail_queue_item,
    get_bounded_run,
    get_connection,
    get_pending_scheduled_runs,
    get_queue_item,
    initialize_schema,
    insert_queue_item,
    is_cancel_requested,
    mark_bounded_run_exhausted,
    provision_bounded_run as _db_provision_bounded_run,
    record_bounded_run_claim,
    release_queue_item,
    set_worker_control,
    update_scheduled_run_status,
)
from hypogeum.failure_policy import FailurePolicy


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
    retry_count: int = 0,
    next_attempt_at: Optional[str] = None,
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
        source:                'rerun' | 'schedule' | 'manual' | 'auto_retry'.
                               Used for claim ordering (reruns are preferred
                               over schedules).
        recovery_from_run_id:  run_id of the failed run this item recovers.
                               Set for source='rerun'/'auto_retry' items.
        retry_count:           Attempt number for auto-retry items (0 for
                               every other source). See _maybe_schedule_retry().
        next_attempt_at:       ISO 8601 UTC — claim_next() will not select
                               this item before this time. None (default)
                               means immediately claimable.

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
            retry_count=retry_count,
            next_attempt_at=next_attempt_at,
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


def provision_bounded_run(
    bounded_run_id: str,
    max_episode_claims: int,
    publishing_allowed: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Explicitly provision a bounded run before any bounded claiming happens
    (CORE INVARIANTS #1). Idempotent for a restart of the same logical
    bounded run (same bounded_run_id + identical parameters) — see
    hypogeum.db.provision_bounded_run() for the exact contract, including
    the BoundedRunError raised on an ambiguous re-provisioning attempt.

    Args:
        bounded_run_id:      Caller-supplied identity. Never generated here.
        max_episode_claims:  Must be a positive integer.
        publishing_allowed:  Defaults to False — a bounded run starts unable
                             to publish unless explicitly authorized.
        db_path:              Override DB path (for tests).

    Returns:
        Dict of the bounded_runs row (freshly created or pre-existing).
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = _db_provision_bounded_run(
            conn,
            bounded_run_id=bounded_run_id,
            max_episode_claims=max_episode_claims,
            publishing_allowed=publishing_allowed,
            created_at=_iso(_now_utc()),
        )
        conn.commit()
        return row
    finally:
        conn.close()


def claim_next_bounded(
    worker_id: str,
    bounded_run_id: Optional[str],
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Atomically claim the next bounded-eligible queued item AND record its
    durable bounded_run_claims ledger row, in one SQLite transaction
    (CORE INVARIANTS #5, #7, #8). See schema.sql's "bounded_runs /
    bounded_run_claims" section for the full contract.

    bounded_run_id must already be explicitly provisioned via
    provision_bounded_run() — this function never creates or mutates a
    bounded_runs row's identity (CORE INVARIANTS #1/#2), only reads it and
    (on the claim that reaches the cap) flips its status to 'exhausted'.

    Only run_queue candidates with a non-NULL episode_id are eligible: the
    ledger's episode_id column is NOT NULL and must be recorded in the same
    transaction as the claim, but episode_id for a topic-backlog-driven item
    is not resolved until well after a plain claim_next() call returns (see
    process_next() Step 4) — so bounded claiming only ever consumes queue
    items that were enqueued for a specific, already-known episode_id (e.g.
    via enqueue(episode_id=...) or the --gated-episode-id CLI path).

    Concurrency (CORE INVARIANTS #6): uses BEGIN IMMEDIATE, not the default
    deferred transaction, so a second concurrent claim attempt (this
    process or another) blocks on SQLite's write lock until this
    transaction commits or rolls back — the read-count-then-insert sequence
    is fully serialized across callers rather than racing optimistically.

    Raises hypogeum.db.BoundedRunError (fail closed — no run_queue claim and
    no ledger row is ever left committed) when: bounded_run_id is missing;
    the bounded run is unknown; its status is not 'active'; its
    max_episode_claims is invalid; it has already reached
    max_episode_claims; or the write-lock transaction cannot be acquired.
    Never silently falls back to unbounded claiming.

    Returns:
        Dict of the claimed run_queue row (same shape as claim_next()), or
        None if no bounded-eligible candidate exists right now — the normal
        "nothing to claim yet" case (queue empty, or every queued item still
        lacks episode_id), not a denial.
    """
    if not bounded_run_id:
        raise BoundedRunError("bounded_run_id is required for bounded claiming")

    now = _iso(_now_utc())
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None  # manual transaction control for BEGIN IMMEDIATE
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise BoundedRunError(
                f"could not acquire bounded-run transaction lock for "
                f"bounded_run_id={bounded_run_id!r}: {exc}"
            ) from exc

        try:
            run = get_bounded_run(conn, bounded_run_id)
            if run is None:
                raise BoundedRunError(f"unknown bounded_run_id: {bounded_run_id!r}")
            if run["status"] != "active":
                raise BoundedRunError(
                    f"bounded_run_id {bounded_run_id!r} is not active "
                    f"(status={run['status']!r})"
                )
            max_claims = run["max_episode_claims"]
            if not isinstance(max_claims, int) or max_claims <= 0:
                raise BoundedRunError(
                    f"bounded_run_id {bounded_run_id!r} has an invalid "
                    f"max_episode_claims={max_claims!r}"
                )

            claimed_count = count_bounded_run_claims(conn, bounded_run_id)
            if claimed_count >= max_claims:
                raise BoundedRunError(
                    f"bounded_run_id {bounded_run_id!r} has already reached "
                    f"max_episode_claims={max_claims}"
                )

            claimed = claim_next_queue_item(conn, worker_id, now, require_episode_id=True)
            if claimed is None or not claimed.get("episode_id"):
                conn.rollback()
                return None

            record_bounded_run_claim(
                conn,
                bounded_run_id=bounded_run_id,
                queue_id=claimed["queue_id"],
                episode_id=claimed["episode_id"],
                claimed_at=now,
            )

            if claimed_count + 1 >= max_claims:
                mark_bounded_run_exhausted(conn, bounded_run_id, now)

            conn.commit()
            return claimed
        except BaseException:
            conn.rollback()
            raise
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

_NULL_CONTEXT_CANCEL_MSG = "Cancelled invalid scheduler queue item"

# A pending scheduled_run promoted later than this many seconds after it was
# created (scheduled_for — tick() time) is tagged source="schedule_missed"
# instead of "schedule" and logged (req 6, Scheduler Integration milestone —
# e.g. the queue was paused, or the worker was down between tick() and this
# promotion). Nothing is ever dropped either way — see docstring below.
_STALE_PROMOTION_GRACE_SECONDS = 300


def consume_scheduled_runs(
    db_path: Optional[Path] = None,
) -> list[str]:
    """
    Promote all pending scheduled_runs to run_queue entries.

    This is the Scheduler's own promotion step (queue.py's "Scheduler
    integration" section) — the only place scheduler-originated work becomes
    a real queue item, and its only responsibility: deciding WHEN work
    should be enqueued. For each scheduled_run with status='pending':
      - If both channel_id and episode_id are NULL: no execution context
        exists — skip promotion, mark the scheduled_run 'cancelled' (the
        queue item would loop forever as no_topic otherwise).
      - Otherwise: call queue.enqueue() (this module's own public entry
        point — never a raw insert) with source="schedule" (or
        "schedule_missed" if this scheduled_run sat pending well past its
        creation time — see _STALE_PROMOTION_GRACE_SECONDS) and mark the
        scheduled_run 'triggered'.

    This function never reads or writes episode_topics, and never claims or
    even looks at any topic — that remains exclusively process_next()'s job
    (see its own docstring's Step 4 for the claim-time target_publish_at
    derivation that replaces this). Peeking at "the next queued topic" here
    was tried and reverted: it's a positional assumption over mutable
    shared state that two independently-timed reads (this promotion vs. the
    later real claim) can disagree on — see the Scheduler Integration
    milestone's race-condition correction for the full analysis.

    Also cancels any existing queued/claimed run_queue rows that have both
    channel_id and episode_id NULL (stale NULL items from prior ticks).

    Does not execute the pipeline. Does not generate content.
    Does not publish. Pure state promotion.

    Args:
        db_path: Override DB path (for tests).

    Returns:
        List of queue_ids created (one per valid promoted scheduled_run).
    """
    now_dt = _now_utc()
    now = _iso(now_dt)
    created_ids: list[str] = []

    conn = get_connection(db_path)
    try:
        # Cancel any stale NULL-context queue items left over from previous ticks.
        conn.execute(
            """
            UPDATE run_queue
               SET status        = 'cancelled',
                   error_message = ?,
                   failed_at     = ?
             WHERE channel_id IS NULL
               AND episode_id IS NULL
               AND status IN ('queued', 'claimed')
            """,
            (_NULL_CONTEXT_CANCEL_MSG, now),
        )
        conn.commit()

        pending = get_pending_scheduled_runs(conn)
    finally:
        conn.close()

    for row in pending:
        conn = get_connection(db_path)
        try:
            if row["channel_id"] is None and row["episode_id"] is None:
                # No execution context — skip promotion, mark scheduled_run cancelled.
                update_scheduled_run_status(
                    conn,
                    scheduled_run_id=row["scheduled_run_id"],
                    status="cancelled",
                    triggered_at=now,
                )
                conn.commit()
                continue
        finally:
            conn.close()

        # Missed-promotion detection: how long this row sat 'pending' before
        # this consume() pass reached it.
        source = "schedule"
        scheduled_for = row["scheduled_for"]
        if scheduled_for:
            delay_seconds = (now_dt - datetime.fromisoformat(scheduled_for)).total_seconds()
            if delay_seconds > _STALE_PROMOTION_GRACE_SECONDS:
                source = "schedule_missed"
                from hypogeum.logger import get_logger
                get_logger(agent="scheduler").warn(
                    "scheduled_run_promoted_late",
                    schedule_id=row["schedule_id"],
                    scheduled_run_id=row["scheduled_run_id"],
                    channel_id=row["channel_id"],
                    scheduled_for=scheduled_for,
                    promoted_at=now,
                    delay_seconds=int(delay_seconds),
                )

        # Enqueue via the canonical entry point — never a raw insert.
        queue_id = enqueue(
            db_path=db_path,
            scheduled_run_id=row["scheduled_run_id"],
            schedule_id=row["schedule_id"],
            channel_id=row["channel_id"],
            episode_id=row["episode_id"],
            priority=0,
            source=source,
        )

        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(
                conn,
                scheduled_run_id=row["scheduled_run_id"],
                status="triggered",
                triggered_at=now,
            )
            conn.commit()
        finally:
            conn.close()

        created_ids.append(queue_id)

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

# Fallback backoff (minutes per retry attempt, 1-indexed) — used only if
# Config.QUEUE_RETRY_BACKOFF_MINUTES is absent/unparseable. The real default
# lives in Config (see hypogeum/config.py) so this is a defensive floor, not
# the source of truth.
_FALLBACK_BACKOFF_MINUTES = [5, 20]


def _resolve_backoff_minutes(config, retry_count: int) -> int:
    """
    Parse config.QUEUE_RETRY_BACKOFF_MINUTES ("5,20") into delay minutes for
    the given 1-indexed retry attempt number. Reuses the last entry if
    retry_count exceeds the parsed list's length — protects against a
    policy shorter than QUEUE_MAX_AUTO_RETRIES.
    """
    raw = getattr(config, "QUEUE_RETRY_BACKOFF_MINUTES", None) if config is not None else None
    minutes_list = list(_FALLBACK_BACKOFF_MINUTES)
    if raw:
        try:
            parsed = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if parsed:
                minutes_list = parsed
        except ValueError:
            pass
    idx = min(max(retry_count, 1), len(minutes_list)) - 1
    return minutes_list[idx]


def _lookup_latest_run_id(episode_id: Optional[str], db_path: Optional[Path]) -> Optional[str]:
    """Most recent runs.run_id for episode_id — the run that just failed."""
    if not episode_id:
        return None
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE episode_id = ? ORDER BY started_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["run_id"] if row else None


def _maybe_schedule_retry(
    exc: Optional[BaseException],
    *,
    channel_id: Optional[str],
    episode_id: Optional[str],
    topic_text: Optional[str],
    retry_count: int,
    priority: int,
    config=None,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Decide whether to schedule a bounded auto-retry for a failed attempt.

    exc: the exception that caused the failure (live process_next() path),
    or None (orphan reconciliation path — infrastructure interruption, not
    a business decision, so always retry-eligible; never TERMINAL).

    Classification never names a concrete exception type — reads
    exc.failure_policy (hypogeum.failure_policy.FailurePolicy), defaulting
    to TRANSIENT when the attribute is absent, so any current or future
    exception class is retryable unless it explicitly opts out.

    Creates both a fresh episode_topics row and a fresh run_queue row (via
    topics.enqueue_topic() + this module's own enqueue()) — mirroring
    exactly how run_dashboard.py::api_run_rerun already reruns an episode,
    so claim_next_topic() reliably resolves the same content on the next
    claim rather than an unrelated backlog topic. Never touches the
    original topic's own release/skip handling — topic-quality
    classification and execution-reliability retries are independent
    concerns.

    Returns True if a retry topic+queue pair was created, False otherwise
    (terminal classification, retry budget exhausted, or missing enough
    context to safely recreate the attempt). Never raises — best-effort,
    matching the surrounding failure-handling code's own philosophy.
    """
    try:
        if exc is not None and getattr(
            exc, "failure_policy", FailurePolicy.TRANSIENT,
        ) == FailurePolicy.TERMINAL:
            return False

        if not channel_id or not episode_id or not topic_text:
            return False

        max_retries = getattr(config, "QUEUE_MAX_AUTO_RETRIES", 2) if config is not None else 2
        if retry_count >= max_retries:
            return False

        next_retry_count = retry_count + 1
        backoff_minutes = _resolve_backoff_minutes(config, next_retry_count)
        next_attempt_at = _iso(_now_utc() + timedelta(minutes=backoff_minutes))
        recovery_from_run_id = _lookup_latest_run_id(episode_id, db_path)

        from hypogeum.topics import enqueue_topic
        enqueue_topic(
            channel_id, topic_text,
            priority=priority,
            source="auto_retry",
            episode_id=episode_id,
            db_path=db_path,
        )
        enqueue(
            db_path=db_path,
            channel_id=channel_id,
            episode_id=episode_id,
            priority=priority,
            source="auto_retry",
            recovery_from_run_id=recovery_from_run_id,
            retry_count=next_retry_count,
            next_attempt_at=next_attempt_at,
        )
        return True
    except Exception:
        return False


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
    pipeline_executor: Optional[Callable[..., None]] = None,
    bounded_run_id: Optional[str] = None,
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
              topic_id is kept for the consume step on success. If the
              claimed item is a scheduled invocation (see
              hypogeum.scheduler.is_scheduled_invocation()), derive a
              deterministic target_publish_at (hypogeum.scheduler.
              compute_next_publish_slot()) for the topic just claimed and
              persist it via hypogeum.topics.set_topic_target_publish_at()
              if not already set — computed here, after the claim, for the
              exact topic claimed, so there is no positional/timing
              assumption about which topic this ends up being (see the
              Scheduler Integration milestone's race-condition correction).
              The computation is anchored to the scheduled_run's own
              scheduled_for (hypogeum.scheduler.get_scheduled_run()), not the
              live clock, so a claim delayed past the intended slot's own
              time-of-day never silently retargets a different calendar
              occurrence. Best-effort: never blocks the claim.
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
        pipeline_executor: Optional callable replacing the direct call to
                         hypogeum.pipeline.run_pipeline() in Step 5. When
                         given, it is called as
                         executor(topic_input=, episode_id=, config=,
                                  channel_id=, cancel_check=, queue_id=,
                                  worker_id=) — two extra keyword arguments
                         beyond the default call, so a gated executor can
                         call queue.release() itself on a transient block
                         (see hypogeum.worker_integration.build_gated_pipeline_executor).
                         When None (default, every existing caller), the
                         call is byte-identical to prior behavior — no
                         extra keywords, calling hypogeum.pipeline.run_pipeline()
                         directly. This is the sole seam for inserting
                         per-run gating (ChannelOperationsProfile /
                         BackwardPlan / publishing platform filtering)
                         into the real claim → topic → pipeline →
                         complete/fail/release lifecycle without
                         duplicating any of it.
        bounded_run_id:  Optional. When given, Step 1 claims via
                         claim_next_bounded() instead of claim_next() — see
                         that function's docstring for the durable claim-cap
                         contract. None (default, every existing caller)
                         preserves prior behavior exactly. A
                         hypogeum.db.BoundedRunError raised by
                         claim_next_bounded() (unknown/inactive bounded run,
                         exhausted cap, invalid limit, or a transaction-lock
                         failure) is caught here and reported as
                         ProcessResult(status="empty", error_message=<reason>)
                         — no claim is made and this never silently falls
                         back to unbounded claiming.

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
    if bounded_run_id is not None:
        try:
            claimed = claim_next_bounded(
                worker_id=worker_id, bounded_run_id=bounded_run_id, db_path=db_path,
            )
        except BoundedRunError as exc:
            return ProcessResult(
                status="empty",
                queue_id=None,
                channel_id=None,
                worker_id=worker_id,
                dry_run=dry_run,
                error_message=str(exc),
            )
    else:
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
    # Guards _maybe_schedule_retry()'s call site against a NameError if an
    # exception is raised before Step 5 assigns the real value below.
    episode_id: Optional[str] = None

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

            # Scheduler Integration: for a scheduled invocation, derive the
            # deterministic publish time now — for the topic just actually
            # claimed, never a topic merely observed earlier — and persist
            # it onto episode_topics if not already set. compute_next_publish_slot()
            # is a pure function of (channel config, from_dt); it does not
            # depend on which topic gets claimed, so there is nothing to
            # get wrong by computing it here instead of at promotion time.
            # from_dt is anchored to the scheduled_run's own scheduled_for
            # (set once by tick(), never mutated afterward) rather than the
            # live wall clock at claim time — otherwise a claim delayed past
            # the intended slot's own time-of-day would silently roll the
            # target forward by a full cadence period, retargeting a
            # different calendar occurrence than the one the Scheduler
            # actually fired for. Using the frozen scheduled_for makes the
            # result identical no matter how long the claim was delayed, and
            # stable across any number of release/reclaim retries of the
            # same scheduled_run.
            # Best-effort: never blocks the claim or the pipeline call —
            # a failure here just means worker_integration.py's existing
            # fail-closed gate behavior applies unchanged.
            from hypogeum.scheduler import is_scheduled_invocation
            if is_scheduled_invocation(claimed) and not topic_row.get("target_publish_at"):
                try:
                    from hypogeum.channel_config import get_channel_operations_profile
                    from hypogeum.scheduler import compute_next_publish_slot, get_scheduled_run
                    from hypogeum.topics import set_topic_target_publish_at

                    profile = get_channel_operations_profile(channel_id, db_path=db_path)
                    if profile is not None:
                        scheduled_run = get_scheduled_run(
                            claimed.get("scheduled_run_id"), db_path=db_path,
                        )
                        anchor_dt = (
                            datetime.fromisoformat(scheduled_run["scheduled_for"])
                            if scheduled_run is not None and scheduled_run.get("scheduled_for")
                            else _now_utc()  # defensive fallback only — should not happen
                        )
                        slot = compute_next_publish_slot(
                            profile.publishing_schedule, profile.timezone, anchor_dt,
                        )
                        if slot is not None:
                            set_topic_target_publish_at(topic_id, _iso(slot), db_path=db_path)
                except Exception as exc:
                    from hypogeum.logger import get_logger
                    get_logger(agent="scheduler").warn(
                        "claim_time_publish_slot_derivation_failed",
                        queue_id=queue_id,
                        channel_id=channel_id,
                        topic_id=topic_id,
                        error=str(exc),
                    )

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

        # Identity contract: topic_id claimed -> episode_id assigned (above) ->
        # episode_topics.episode_id persisted (here) -> run_queue.episode_id
        # persisted (here) -> target_publish_at resolved by episode_id
        # (hypogeum.worker_integration.lookup_episode_target_publish_at) ->
        # resolve_backward_plan() -> run_pipeline(). target_publish_at is
        # written keyed by topic_id (set_topic_target_publish_at, Step 4
        # above) but read keyed by episode_id — the two only refer to the
        # same row once episode_id is persisted onto the topic here. Future
        # code must never write a value keyed by topic_id and read it keyed
        # by episode_id without this mapping existing first.
        if using_backlog and topic_id is not None and _topic_ep_id is None:
            try:
                from hypogeum.topics import set_topic_episode_id
                set_topic_episode_id(topic_id, episode_id, db_path=db_path)
            except Exception as exc:
                from hypogeum.logger import get_logger
                get_logger(agent="scheduler").warn(
                    "claim_time_episode_id_persist_failed",
                    queue_id=queue_id, channel_id=channel_id, topic_id=topic_id,
                    episode_id=episode_id, error=str(exc),
                )

            try:
                from hypogeum.db import get_connection as _gc, set_queue_item_episode_id
                _conn = _gc(db_path)
                try:
                    set_queue_item_episode_id(_conn, queue_id, episode_id)
                    _conn.commit()
                finally:
                    _conn.close()
            except Exception as exc:
                from hypogeum.logger import get_logger
                get_logger(agent="scheduler").warn(
                    "claim_time_queue_episode_id_persist_failed",
                    queue_id=queue_id, channel_id=channel_id, topic_id=topic_id,
                    episode_id=episode_id, error=str(exc),
                )

            # Regression guard: if this topic now carries a target_publish_at
            # (already set, or derived in Step 4 above), the identity chain
            # just established should let the downstream lookup resolve it.
            # Passive only — never raises, retries, or blocks the claim.
            try:
                from hypogeum.topics import get_topic
                _current_topic = get_topic(topic_id, db_path=db_path)
                if _current_topic and _current_topic.get("target_publish_at"):
                    from hypogeum.worker_integration import lookup_episode_target_publish_at
                    if lookup_episode_target_publish_at(episode_id, db_path=db_path) is None:
                        from hypogeum.logger import get_logger
                        get_logger(agent="scheduler").warn(
                            "target_publish_at_identity_contract_broken",
                            queue_id=queue_id, channel_id=channel_id, topic_id=topic_id,
                            episode_id=episode_id,
                            topic_target_publish_at=_current_topic.get("target_publish_at"),
                            topic_episode_id=_current_topic.get("episode_id"),
                        )
            except Exception:
                pass

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

        if pipeline_executor is not None:
            pipeline_executor(
                topic_input=topic_text,
                episode_id=episode_id,
                config=config,
                channel_id=channel_id,
                cancel_check=_cancel_check,
                queue_id=queue_id,
                worker_id=worker_id,
            )
        else:
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
        # Unaffected by and independent of the auto-retry decision below —
        # topic quality and execution reliability are different concerns.
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

        # Auto-retry (Roadmap priority 3 — recovery hardening): bounded,
        # backed-off, execution-level retry — see _maybe_schedule_retry().
        # Independent of the topic hygiene above; never raises.
        retry_scheduled = _maybe_schedule_retry(
            exc,
            channel_id=channel_id,
            episode_id=episode_id,
            topic_text=topic_text,
            retry_count=claimed.get("retry_count") or 0,
            priority=claimed.get("priority") or 0,
            config=config,
            db_path=db_path,
        )
        if retry_scheduled:
            from hypogeum.logger import get_logger
            get_logger(agent="queue").info(
                "queue_item_retry_scheduled",
                queue_id=queue_id, episode_id=episode_id,
            )
        elif getattr(exc, "failure_policy", FailurePolicy.TRANSIENT) != FailurePolicy.TERMINAL:
            from hypogeum.logger import get_logger
            get_logger(agent="queue").info(
                "queue_item_retry_exhausted",
                queue_id=queue_id, episode_id=episode_id,
                retry_count=claimed.get("retry_count") or 0,
            )

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
    pipeline_executor: Optional[Callable[..., None]] = None,
    bounded_run_id: Optional[str] = None,
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
        pipeline_executor:     Forwarded unchanged to process_next()'s own
                               pipeline_executor parameter — see there for
                               the full contract. None (default) preserves
                               prior behavior exactly.
        bounded_run_id:        Forwarded unchanged to process_next()'s own
                               bounded_run_id parameter. None (default)
                               preserves prior behavior exactly.

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
                pipeline_executor=pipeline_executor,
                bounded_run_id=bounded_run_id,
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
