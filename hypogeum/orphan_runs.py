"""
hypogeum/orphan_runs.py — Orphaned run reconciliation

Detects pipeline runs stuck in status='running' with no active worker
and marks them failed. Checkpoints on disk are never touched.

A run is orphaned when:
  - No worker process is alive (PID check), OR
  - A worker process exists but its DB status is not 'running' or 'cancelling'
    (worker is idle/claiming — it is not executing any pipeline right now).

Called from:
  - Dashboard startup
  - Worker startup (before the loop begins)
  - GET /runs page
  - GET /api/worker/status

Public API:
    ORPHAN_ERROR      — canonical error string written to runs.error_message
    reconcile_orphaned_runs(db_path, worker_process_alive, worker_db_status)
        -> list[str]   (orphaned run_ids, already marked failed in DB)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ORPHAN_ERROR = "Run orphaned — worker not active"

# Worker DB statuses that indicate the worker is actively executing a pipeline.
# Any other status means the worker is not running a run right now.
_ACTIVE_PIPELINE_STATUSES = frozenset({"running", "cancelling"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_ever_had_a_lease(run_id: str, db_path: Optional[Path]) -> bool:
    """Whether any runtime_leases row exists for this run, active or not."""
    from hypogeum.db import get_connection, get_lease
    try:
        conn = get_connection(db_path)
        try:
            return get_lease(conn, f"run:{run_id}") is not None
        finally:
            conn.close()
    except Exception:
        return False


def reconcile_orphaned_runs(
    db_path: Optional[Path] = None,
    worker_process_alive: bool = False,
    worker_db_status: str = "stopped",
) -> list[str]:
    """
    Find all runs with status='running' that are not legitimately owned
    right now.

    Ownership is determined per-run via the durable runtime_leases lease
    each run acquires at register_run() and renews (heartbeat) at every
    stage boundary (see hypogeum.run_registry.acquire_run_lease() /
    renew_run_lease() / run_lease_is_live()) -- an active, unexpired lease
    means SOME process legitimately owns this run right now, regardless of
    whether it is the one worker daemon this function's caller happens to
    be tracking. This replaces the pre-2026-08-17-fix behavior, which
    treated "not the one worker's own in-memory-tracked status" as
    equivalent to "orphaned" -- misclassifying any live, legitimate,
    out-of-band run_pipeline() call (an entrypoint other than the tracked
    worker daemon) as orphaned while it was genuinely still executing.

    worker_process_alive/worker_db_status remain as a fallback signal ONLY
    for runs with no lease row at all (registered before this fix, or a
    lease-acquisition failure at registration time) -- never authoritative
    once a lease exists.

    Marks each orphaned run as 'failed' in the DB with ORPHAN_ERROR.
    Stage executions and checkpoints on disk are never modified.

    Returns list of run_ids that were orphaned (may be empty).
    """
    from hypogeum.db import get_connection, initialize_schema
    from hypogeum.run_registry import run_lease_is_live

    legacy_worker_is_running_pipeline = (
        worker_process_alive
        and worker_db_status in _ACTIVE_PIPELINE_STATUSES
    )

    try:
        initialize_schema(db_path)
    except Exception:
        return []

    try:
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT run_id, episode_id FROM runs WHERE status = 'running'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    orphaned_ids: list[str] = []
    orphaned_episode_ids: list[str] = []
    now = _now_iso()
    worker_episode = _worker_active_episode(db_path) if legacy_worker_is_running_pipeline else None

    for row in rows:
        run_id = row["run_id"]
        episode_id = row["episode_id"]

        if run_lease_is_live(run_id, at=now, db_path=db_path):
            continue  # legitimately owned right now -- never orphaned

        # The lease is wall-clock: a Mac asleep longer than its TTL wakes
        # with the lease "expired" while the worker process -- suspended,
        # not dead -- carries on with this very run. The live worker saying
        # it is running THIS episode wins (2026-10-05).
        if worker_episode and worker_episode == episode_id:
            continue

        has_lease_row = _has_ever_had_a_lease(run_id, db_path)
        if not has_lease_row and legacy_worker_is_running_pipeline:
            # No lease was ever registered for this run (pre-fix run, or a
            # best-effort acquisition failure) -- fall back to the old
            # heuristic rather than treating absence-of-lease as proof of
            # death on its own.
            continue

        try:
            _mark_orphaned(run_id, episode_id, now, db_path)
            orphaned_ids.append(run_id)
            if episode_id:
                orphaned_episode_ids.append(episode_id)
        except Exception:
            pass

    # Reconcile run_queue / stage_executions too — same invariant as above
    # (no worker is actively running a pipeline right now) means neither
    # table can legitimately hold a 'claimed'/'running' row at this
    # instant. Best-effort: a failure here must never affect the runs-table
    # result already computed above. Runs even when `rows` was empty, so
    # the Tier 2 residual sweep below still fires for a queue item whose
    # owning worker crashed before its runs row was ever registered.
    try:
        _reconcile_queue_and_stage_state(
            orphaned_episode_ids, now, db_path,
            skip_tier2=legacy_worker_is_running_pipeline,
        )
    except Exception:
        pass

    return orphaned_ids


def _worker_active_episode(db_path: Optional[Path]) -> Optional[str]:
    """The episode the worker says it is producing right now, or None."""
    from hypogeum.db import get_connection, get_worker_control
    try:
        qid = get_worker_control("active_queue_id", "", db_path=db_path)
        if not qid:
            return None
        conn = get_connection(db_path)
        try:
            r = conn.execute("SELECT episode_id FROM run_queue WHERE queue_id = ?", (qid,)).fetchone()
            return r["episode_id"] if r and r["episode_id"] else None
        finally:
            conn.close()
    except Exception:
        return None


def _reconcile_queue_and_stage_state(
    orphaned_episode_ids: list,
    now: str,
    db_path: Optional[Path],
    skip_tier2: bool = False,
) -> None:
    """
    Tier 1: reconcile run_queue/stage_executions rows attributable to the
    orphaned runs rows just identified — the preferred path, since it
    produces precise evidence (which run each stuck item belonged to).
    Always runs -- driven entirely by orphaned_episode_ids, which is
    correctly empty whenever nothing was actually orphaned.

    Tier 2: reconcile any run_queue row still 'claimed' afterward. A
    worker can crash after claiming a queue item but before its runs row
    is even inserted, leaving a row with no orphaned run to associate it
    with, so any row still 'claimed' at this point is unconditionally
    stale -- UNLESS skip_tier2 is True, meaning the caller's legacy
    worker-tracking signal says a worker IS actively running a pipeline
    right now (the per-run lease loop above only ever falls back to that
    legacy signal for runs with no lease row at all -- see
    reconcile_orphaned_runs()'s own docstring), in which case a 'claimed'
    row could legitimately belong to that in-progress work and must not be
    swept.

    Never raises past this function — the caller treats it as best-effort.
    """
    from hypogeum.db import (
        fail_all_claimed_queue_items,
        fail_orphaned_queue_items_for_episodes,
        get_connection,
        get_running_stage_execution_ids_for_episodes,
        update_stage_execution_failed,
    )
    from hypogeum.logger import get_logger

    log = get_logger(agent="orphan_runs")

    conn = get_connection(db_path)
    try:
        tier1_queue_ids = fail_orphaned_queue_items_for_episodes(
            conn, orphaned_episode_ids, now, ORPHAN_ERROR,
        )
        stage_exec_ids = get_running_stage_execution_ids_for_episodes(
            conn, orphaned_episode_ids,
        )
        for stage_exec_id in stage_exec_ids:
            try:
                update_stage_execution_failed(conn, stage_exec_id, now, ORPHAN_ERROR)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()

    if tier1_queue_ids:
        log.info(
            "orphaned_queue_items_reconciled",
            queue_ids=tier1_queue_ids,
            episode_ids=orphaned_episode_ids,
        )
    if stage_exec_ids:
        log.info(
            "orphaned_stage_executions_reconciled",
            stage_exec_ids=stage_exec_ids,
            episode_ids=orphaned_episode_ids,
        )

    tier2_queue_ids: list = []
    if not skip_tier2:
        conn = get_connection(db_path)
        try:
            tier2_queue_ids = fail_all_claimed_queue_items(conn, now, ORPHAN_ERROR)
            conn.commit()
        finally:
            conn.close()

    if tier2_queue_ids:
        log.warn(
            "orphaned_queue_item_reconciled_unassociated",
            queue_ids=tier2_queue_ids,
        )

    # Tier 3: auto-retry (Roadmap priority 3 — recovery hardening). An
    # orphan is, by definition, an infrastructure interruption (crash,
    # kill, power loss) — never a business/content decision — so every
    # orphaned item is retry-eligible, bounded by the same cap/backoff as
    # a live failure (hypogeum.queue._retry_eligible()/
    # _schedule_retry_queue_item(), called here with exc=None). Additive
    # to Tier 1/Tier 2 above; never replaces them.
    _schedule_orphan_retries(tier1_queue_ids + tier2_queue_ids, db_path, log)


def _schedule_orphan_retries(
    queue_ids: list,
    db_path: Optional[Path],
    log,
) -> None:
    """Best-effort: attempt an auto-retry for each just-orphaned queue item."""
    if not queue_ids:
        return

    from hypogeum.config import Config
    from hypogeum.db import get_connection
    from hypogeum.queue import _retry_eligible, _schedule_retry_queue_item
    from hypogeum.topics import mark_topic_retry

    try:
        config = Config()
    except Exception:
        config = None

    for queue_id in queue_ids:
        try:
            conn = get_connection(db_path)
            try:
                row = conn.execute(
                    "SELECT channel_id, episode_id, priority, retry_count "
                    "FROM run_queue WHERE queue_id = ?",
                    (queue_id,),
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                continue

            channel_id = row["channel_id"]
            episode_id = row["episode_id"]
            topic_text = _lookup_topic_text(episode_id, db_path)

            eligible = _retry_eligible(
                None,  # orphan — no exception object, always retry-eligible
                channel_id=channel_id,
                episode_id=episode_id,
                topic_text=topic_text,
                retry_count=row["retry_count"] or 0,
                config=config,
            )

            # Reuse the SAME topic row the orphaned run was working from —
            # never a freshly-enqueued duplicate (see topics.mark_topic_retry()'s
            # own docstring). A worker that crashes mid-run leaves its topic
            # row 'claimed' (never released or consumed); mark it as the
            # retry identity directly. If no such row is found (already
            # released/reclaimed by another path), there is nothing to mark
            # — the run_queue side below is still scheduled as long as one
            # is genuinely eligible, so a later claim resolves a topic
            # normally rather than through this recovery path.
            if eligible and episode_id:
                topic_id = _find_claimed_topic_id_for_episode(episode_id, db_path)
                if topic_id is not None:
                    mark_topic_retry(topic_id, db_path=db_path)

            scheduled = (
                _schedule_retry_queue_item(
                    channel_id=channel_id,
                    episode_id=episode_id,
                    retry_count=row["retry_count"] or 0,
                    priority=row["priority"] or 0,
                    config=config,
                    db_path=db_path,
                )
                if eligible and channel_id and episode_id
                else False
            )
            if scheduled:
                log.info(
                    "orphaned_queue_item_retry_scheduled",
                    queue_id=queue_id, episode_id=episode_id,
                )
            else:
                log.info(
                    "orphaned_queue_item_retry_exhausted",
                    queue_id=queue_id, episode_id=episode_id,
                    retry_count=row["retry_count"] or 0,
                )
        except Exception:
            pass  # never let retry scheduling affect reconciliation's own result


def _find_claimed_topic_id_for_episode(
    episode_id: str,
    db_path: Optional[Path],
) -> Optional[str]:
    """
    Find the 'claimed' episode_topics row for this episode_id, if any — the
    row an orphaned worker was working from when it crashed. Used so
    Tier 3 retry scheduling reuses this SAME row (topics.mark_topic_retry())
    instead of creating a second one. None if no 'claimed' row exists
    (already released/reclaimed by another path, or never had one).
    """
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT topic_id FROM episode_topics WHERE episode_id = ? AND status = 'claimed' "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        return row["topic_id"] if row is not None else None
    finally:
        conn.close()


def _lookup_topic_text(episode_id: Optional[str], db_path: Optional[Path]) -> Optional[str]:
    """
    Recover the topic content an orphaned episode was about — needed for
    retry-eligibility classification (see hypogeum.queue._retry_eligible()).
    Prefers episode_topics (the identity contract established by H2 links
    episode_id -> topic_text directly); falls back to episodes.topic.
    Best-effort — None on any miss, which _retry_eligible() treats as
    "cannot retry safely."
    """
    if not episode_id:
        return None
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT topic_text FROM episode_topics WHERE episode_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if row is not None and row["topic_text"]:
            return row["topic_text"]
        row = conn.execute(
            "SELECT topic FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return row["topic"] if row is not None else None
    finally:
        conn.close()


def _mark_orphaned(
    run_id: str,
    episode_id: str,
    now: str,
    db_path: Optional[Path],
) -> None:
    """
    Mark one run as failed with ORPHAN_ERROR.

    Tries the run_registry state machine first (records a transition row).
    Falls back to a direct UPDATE if the state machine rejects the transition
    (e.g., run was already in a terminal state due to a race condition).
    """
    try:
        from hypogeum.run_registry import fail_run
        fail_run(
            run_id=run_id,
            episode_id=episode_id,
            error_message=ORPHAN_ERROR,
            db_path=db_path,
        )
    except Exception:
        # Direct fallback: bypass state machine to ensure the row is corrected.
        try:
            from hypogeum.db import get_connection
            conn = get_connection(db_path)
            try:
                conn.execute(
                    "UPDATE runs SET status='failed', completed_at=?, exit_code=1, "
                    "error_message=? WHERE run_id=? AND status='running'",
                    (now, ORPHAN_ERROR, run_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
