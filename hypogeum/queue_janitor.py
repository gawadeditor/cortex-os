"""
hypogeum/queue_janitor.py — automatic retention on terminal queue/run rows.

The readiness audit had to interpret ~47 historical run_queue / runs rows
by hand. This keeps those tables to a working window: terminal-state rows
(completed / failed / cancelled) older than the retention window are
deleted, but ONLY when the row's episode is either absent or already in a
done/published state — an in-flight or unresolved episode's history is
never touched.

`stage_executions` and `costs` rows are left alone (the audit trail lives
there); `cleanup_factory_state.py` is the deliberate, operator-run deep
clean. This is the light, automatic sweep.

Public API:
    prune_terminal_rows(*, retention_days=45, db_path=None, dry_run=False) -> dict
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection
from hypogeum.logger import get_logger

log = get_logger()

_TERMINAL_QUEUE = ("completed", "failed", "cancelled")
_TERMINAL_RUNS = ("complete", "completed", "failed", "cancelled")


def _done_or_missing_episode_ids(conn) -> set:
    ids: set = {None, ""}
    for sql in (
        "SELECT episode_id FROM episodes WHERE status IN ('complete','completed','published')",
        "SELECT DISTINCT episode_id FROM publication_intents WHERE status = 'succeeded'",
    ):
        try:
            ids.update(r[0] for r in conn.execute(sql))
        except Exception:  # noqa: BLE001
            continue
    return ids


def prune_terminal_rows(
    *, retention_days: int = 45, db_path: Optional[Path] = None, dry_run: bool = False,
) -> dict:
    """Delete terminal-state run_queue / runs rows older than the window whose
    episode is done or missing. Never raises."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()
    out = {"queue_pruned": 0, "runs_pruned": 0, "cutoff": cutoff, "dry_run": dry_run}
    try:
        conn = get_connection(db_path)
    except Exception as exc:  # noqa: BLE001
        log.error("queue_janitor_open_error", error=str(exc))
        return out
    try:
        keep_ep = _done_or_missing_episode_ids(conn)

        q_marks = ",".join("?" * len(_TERMINAL_QUEUE))
        q_rows = conn.execute(
            f"SELECT queue_id, episode_id FROM run_queue "
            f"WHERE status IN ({q_marks}) "
            f"AND COALESCE(completed_at, failed_at, claimed_at, queued_at) < ?",
            (*_TERMINAL_QUEUE, cutoff),
        ).fetchall()
        q_kill = [r["queue_id"] for r in q_rows if r["episode_id"] in keep_ep]

        r_marks = ",".join("?" * len(_TERMINAL_RUNS))
        r_rows = conn.execute(
            f"SELECT run_id, episode_id FROM runs "
            f"WHERE status IN ({r_marks}) "
            f"AND COALESCE(completed_at, started_at) < ?",
            (*_TERMINAL_RUNS, cutoff),
        ).fetchall()
        r_kill = [r["run_id"] for r in r_rows if r["episode_id"] in keep_ep]

        out["queue_pruned"] = len(q_kill)
        out["runs_pruned"] = len(r_kill)

        if not dry_run and (q_kill or r_kill):
            if q_kill:
                conn.executemany("DELETE FROM run_queue WHERE queue_id = ?",
                                 [(x,) for x in q_kill])
            if r_kill:
                conn.executemany("DELETE FROM runs WHERE run_id = ?",
                                 [(x,) for x in r_kill])
            conn.commit()
            log.info("queue_janitor", queue_pruned=len(q_kill), runs_pruned=len(r_kill))
    except Exception as exc:  # noqa: BLE001
        log.error("queue_janitor_error", error=str(exc))
    finally:
        conn.close()
    return out
