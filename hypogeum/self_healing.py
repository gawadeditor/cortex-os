"""
hypogeum/self_healing.py — Recovery & Self-Healing (Phase 3 Production
Hardening, P0.4)

Verification and extension of EXISTING recovery mechanisms only — this
module does not rebuild:
  - Worker/scheduler auto-restart — already handled by launchd
    supervision, validated against a real hard-kill
    (docs/operations/BURN_IN_READINESS_AUDIT.md).
  - Runtime-state recovery — checkpoint-resume (Audio/Assembly/
    Publishing) and bounded auto-retry, already shipped (faa8388, b08f8f7).

Confirmed gap this module closes (BURN_IN_READINESS_AUDIT.md,
2026-07-26): `hypogeum/orphan_runs.py`'s reconcile_orphaned_runs() ("H1")
only runs at four trigger moments — dashboard startup, worker startup,
GET /runs, GET /api/worker/status — never periodically. The audit found
6 `stage_executions` rows stuck in 'running' with an already-failed
parent `runs` row, predating H1 ever reaching them, because none of
those four triggers happened to fire in the interim.

periodic_check() is intended to be called once per run_worker.py loop
iteration — which already happens every `interval` seconds regardless of
queue activity — so an orphan is caught within one interval instead of
waiting on a human page load or a process restart. It calls the
existing, unmodified reconcile_orphaned_runs(); this module adds no new
reconciliation logic of its own for that part.

Also wires exhausted-retry escalation into Runtime Observability's
alerting (P0.3) — a give-up event (hypogeum.queue._retry_eligible()
declining to reschedule because QUEUE_MAX_AUTO_RETRIES is reached) must
never be silent. Reuses the same hypogeum.ois.emit_signal() substrate as
P0.3 — no parallel alerting path.

Public API:
    periodic_check(db_path=None, config=None, worker_process_alive=True,
                    worker_db_status="idle") -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import get_connection
from hypogeum.ois import emit_signal

_SOURCE_SYSTEM = "self_healing"


def _resolve_config(config):
    if config is not None:
        return config
    try:
        return Config()
    except Exception:
        return None


def periodic_check(
    db_path: Optional[Path] = None,
    config=None,
    worker_process_alive: bool = True,
    worker_db_status: str = "idle",
) -> dict:
    """
    Run one self-healing sweep: orphan reconciliation (existing H1
    mechanism, called periodically instead of only at the four prior
    trigger moments) plus exhausted-retry alerting.

    Safe to call from inside a live worker's own loop: pass the calling
    worker's own current worker_db_status. Call this BEFORE claiming any
    new work each iteration (i.e. while status is still 'idle') — at that
    moment no run this worker owns can legitimately be 'running' in the
    DB yet, so any row still 'running'/'claimed' belongs to a different,
    dead worker instance and is safely reconciled. Calling it mid-pipeline
    (worker_db_status='running') is intentionally a no-op — see
    reconcile_orphaned_runs()'s own worker_is_running_pipeline guard.
    """
    cfg = _resolve_config(config)

    from hypogeum.orphan_runs import reconcile_orphaned_runs
    orphaned_ids = reconcile_orphaned_runs(
        db_path=db_path,
        worker_process_alive=worker_process_alive,
        worker_db_status=worker_db_status,
    )

    alerted = _alert_on_exhausted_retries(db_path=db_path, config=cfg)

    return {
        "orphaned_run_ids": orphaned_ids,
        "retry_exhausted_alerted": alerted,
    }


def _alert_on_exhausted_retries(db_path: Optional[Path], config) -> list:
    """
    Find run_queue items whose bounded auto-retry is exhausted
    (status='failed' and retry_count >= QUEUE_MAX_AUTO_RETRIES) and emit
    an OIS signal for each — deduplicated per channel via
    dedup_any_open=True (the same pattern hypogeum/ois.py already uses
    for persistent conditions like credential_failure), so a channel with
    an unresolved exhausted item is not re-notified every loop iteration,
    but a second exhausted item on a channel whose prior signal was
    resolved does alert again.
    """
    max_retries = getattr(config, "QUEUE_MAX_AUTO_RETRIES", 2) if config else 2

    try:
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT queue_id, channel_id, episode_id, retry_count "
                "FROM run_queue WHERE status = 'failed' AND retry_count >= ?",
                (max_retries,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    alerted = []
    for row in rows:
        message = (
            f"Queue item {row['queue_id']} (episode {row['episode_id']}) "
            f"exhausted auto-retry at {row['retry_count']} attempts "
            f"(limit {max_retries}) — will not retry automatically."
        )
        emit_signal(
            signal_type="retry_exhausted",
            severity="WARNING",
            message=message,
            channel_id=row["channel_id"],
            source_system=_SOURCE_SYSTEM,
            blocking=False,
            episode_id=row["episode_id"],
            db_path=db_path,
            config=config,
            dedup_any_open=True,
        )
        alerted.append(row["queue_id"])

    return alerted
