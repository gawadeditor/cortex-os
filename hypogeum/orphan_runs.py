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

    if not rows:
        return []

    orphaned_ids: list[str] = []
    now = _now_iso()

    for row in rows:
        run_id = row["run_id"]
        episode_id = row["episode_id"]
        try:
            _mark_orphaned(run_id, episode_id, now, db_path)
            orphaned_ids.append(run_id)
        except Exception:
            pass

    return orphaned_ids


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
