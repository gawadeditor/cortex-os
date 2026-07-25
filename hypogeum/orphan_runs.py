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


def reconcile_orphaned_runs(
    db_path: Optional[Path] = None,
    worker_process_alive: bool = False,
    worker_db_status: str = "stopped",
) -> list[str]:
    """
    Find all runs with status='running' that have no active worker.

    worker_process_alive: True if the worker OS process is alive (PID check).
    worker_db_status:     Value of worker_control['worker_status'] in DB.

    Orphan condition (either is enough):
      - worker_process_alive is False
      - worker_db_status not in {'running', 'cancelling'}

    Marks each orphaned run as 'failed' in the DB with ORPHAN_ERROR.
    Stage executions and checkpoints on disk are never modified.

    Returns list of run_ids that were orphaned (may be empty).
    """
    from hypogeum.db import get_connection, initialize_schema

    worker_is_running_pipeline = (
        worker_process_alive
        and worker_db_status in _ACTIVE_PIPELINE_STATUSES
    )
    if worker_is_running_pipeline:
        return []

    try:
        initialize_schema(db_path)
    except Exception:
        return []

    try:
        from hypogeum.db import get_connection
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

    for row in rows:
        run_id = row["run_id"]
        episode_id = row["episode_id"]
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
        _reconcile_queue_and_stage_state(orphaned_episode_ids, now, db_path)
    except Exception:
        pass

    return orphaned_ids


def _reconcile_queue_and_stage_state(
    orphaned_episode_ids: list,
    now: str,
    db_path: Optional[Path],
) -> None:
    """
    Tier 1: reconcile run_queue/stage_executions rows attributable to the
    orphaned runs rows just identified — the preferred path, since it
    produces precise evidence (which run each stuck item belonged to).

    Tier 2: reconcile any run_queue row still 'claimed' afterward. A
    worker can crash after claiming a queue item but before its runs row
    is even inserted, leaving a row with no orphaned run to associate it
    with. The caller has already established no worker is actively
    running a pipeline right now, so any row still 'claimed' at this point
    is unconditionally stale — this tier exists so such a row is still
    reconciled (never invisibly claimed), just logged separately so the
    evidence honestly distinguishes "attributed" from "unattributed."

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
    # a live failure (hypogeum.queue._maybe_schedule_retry(), called here
    # with exc=None). Additive to Tier 1/Tier 2 above; never replaces them.
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
    from hypogeum.queue import _maybe_schedule_retry

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

            scheduled = _maybe_schedule_retry(
                None,  # orphan — no exception object, always retry-eligible
                channel_id=channel_id,
                episode_id=episode_id,
                topic_text=topic_text,
                retry_count=row["retry_count"] or 0,
                priority=row["priority"] or 0,
                config=config,
                db_path=db_path,
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


def _lookup_topic_text(episode_id: Optional[str], db_path: Optional[Path]) -> Optional[str]:
    """
    Recover the topic content an orphaned episode was about — needed to
    recreate an equivalent episode_topics row for the retry (see
    hypogeum.queue._maybe_schedule_retry()). Prefers episode_topics (the
    identity contract established by H2 links episode_id -> topic_text
    directly); falls back to episodes.topic. Best-effort — None on any
    miss, which _maybe_schedule_retry() treats as "cannot retry safely."
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
