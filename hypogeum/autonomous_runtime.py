"""
hypogeum/autonomous_runtime.py — Minimum Autonomous Runtime for ONE channel
(ADR-016 §4).

Composes primitives that already exist -- it is not a parallel runtime:

    hypogeum.scheduler                      cadence / due detection
    hypogeum.runtime_admission              fenced admission + Operating Grant binding
    hypogeum.queue                          the one queue lifecycle
    hypogeum.worker_integration             the one gated pipeline executor
    hypogeum.pipeline.run_pipeline          the frozen 10-agent pipeline
    hypogeum.db.acquire/renew/release_lease fencing-token worker lease

One `run_autonomous_cycle()` call is one wake of the runtime for one channel.
The surrounding daemon cadence (sleep interval, SIGTERM handling) stays in
run_worker.py -- this module owns only: state, admission, heartbeat, the
handoff to the existing executor, checkpoint-aware resume, and stopping at
`ready_to_publish` WITHOUT publishing.

Runtime states (persisted on the admission row + derived):
    idle | due | admitted | running | waiting_retryable | blocked
    | ready_to_publish | completed | failed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from hypogeum.db import get_connection

__all__ = [
    "RuntimeState",
    "CycleResult",
    "is_channel_due",
    "run_autonomous_cycle",
]


class RuntimeState(str, Enum):
    IDLE = "idle"
    DUE = "due"
    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING_RETRYABLE = "waiting_retryable"
    BLOCKED = "blocked"
    READY_TO_PUBLISH = "ready_to_publish"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class CycleResult:
    channel_id: str
    state: RuntimeState
    admission_id: Optional[str] = None
    episode_id: Optional[str] = None
    detail: str = ""
    created_admission: bool = False
    invoked_pipeline: bool = False


# ---------------------------------------------------------------------------
# Due detection -- reuse the scheduler's cadence primitives
# ---------------------------------------------------------------------------

def is_channel_due(
    channel_id: str, *, now: Optional[datetime] = None, db_path: Optional[Path] = None,
) -> bool:
    """A channel is due when it is operator-enabled with a production
    cadence AND there is no active admission for the current channel-week
    yet AND (no prior completed episode this week, using the weekly
    window as the cadence boundary for the Minimum Autonomous Runtime).
    The Channel Operations Profile remains the schedule authority -- this
    function never changes cadence or the schedule."""
    from hypogeum import weekly_budget
    from hypogeum.channel_config import get_channel_operations_profile
    from hypogeum.runtime_admission import admission_id_for, get_admission_by_id

    now = now or datetime.now(timezone.utc)
    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        return False
    if not bool(getattr(profile, "operator_enabled", False)):
        return False
    if not (getattr(profile, "production_cadence", None) or {}):
        return False

    conn = get_connection(db_path)
    try:
        tz_name, _ = weekly_budget.resolve_channel_weekly_policy(conn, channel_id)
    finally:
        conn.close()
    window = weekly_budget.compute_weekly_window(now, tz_name)
    week_ref = weekly_budget.week_scope_ref(channel_id, window)
    adm = get_admission_by_id(admission_id_for(channel_id, week_ref), db_path=db_path)
    if adm is not None and adm.status in ("admitted", "plan_upgraded", "completed"):
        return False
    return True


# ---------------------------------------------------------------------------
# One runtime cycle
# ---------------------------------------------------------------------------

def run_autonomous_cycle(
    channel_id: str,
    *,
    config,
    worker_id: str,
    now: Optional[datetime] = None,
    db_path: Optional[Path] = None,
    pipeline_runner: Optional[Callable[..., object]] = None,
    log=None,
    lease_ttl_seconds: int = 900,
) -> CycleResult:
    """One wake of the Minimum Autonomous Runtime for `channel_id`.

    - No episode due -> IDLE, nothing created.
    - Due, no active admission -> admit (fenced, Operating-Grant-bound,
      PRE_PLAN-reserved), enqueue once -> ADMITTED.
    - Active admission -> heartbeat the fenced lease, then invoke the
      pipeline via `pipeline_runner` (defaults to the real gated
      executor) with publishing DISABLED. On success -> READY_TO_PUBLISH.
      A restart re-enters here and resumes from the last verified
      checkpoint instead of creating a second execution.
    - Any AdmissionError -> BLOCKED (fail closed, diagnosable, nothing
      half-created).
    """
    from hypogeum import runtime_admission as _adm
    from hypogeum import weekly_budget
    from hypogeum.channel_config import get_channel_operations_profile

    now = now or datetime.now(timezone.utc)

    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None or not bool(getattr(profile, "operator_enabled", False)):
        return CycleResult(channel_id, RuntimeState.IDLE, detail="channel not operator-enabled")

    conn = get_connection(db_path)
    try:
        tz_name, _ = weekly_budget.resolve_channel_weekly_policy(conn, channel_id)
    finally:
        conn.close()
    window = weekly_budget.compute_weekly_window(now, tz_name)
    week_ref = weekly_budget.week_scope_ref(channel_id, window)
    admission_id = _adm.admission_id_for(channel_id, week_ref)
    existing = _adm.get_admission_by_id(admission_id, db_path=db_path)

    created = False
    if existing is None or not existing.is_active:
        if existing is not None and existing.status == "completed":
            return CycleResult(channel_id, RuntimeState.COMPLETED, admission_id=admission_id,
                               episode_id=existing.episode_id, detail="episode already completed this week")
        if not is_channel_due(channel_id, now=now, db_path=db_path):
            return CycleResult(channel_id, RuntimeState.IDLE, detail="no episode due")
        try:
            record = _adm.admit_channel_execution(
                channel_id, config=config, worker_id=worker_id, log=log, now=now,
                lease_ttl_seconds=lease_ttl_seconds, db_path=db_path,
            )
            created = True
        except _adm.AdmissionError as exc:
            return CycleResult(channel_id, RuntimeState.BLOCKED, admission_id=admission_id,
                               detail=f"{type(exc).__name__}: {exc}")
    else:
        record = existing
        # Heartbeat the fenced lease for the resuming worker. If this
        # worker still holds it -> renew. If the lease has simply expired
        # (a long gap / process restart) -> the replay path of
        # admit_channel_execution re-acquires it with an incremented
        # fencing token for THIS worker, replaying the same execution
        # identity. If a DIFFERENT live worker holds it -> fail closed.
        try:
            _adm.renew_admission_lease(
                record.admission_id, worker_id, now=now,
                lease_ttl_seconds=lease_ttl_seconds, db_path=db_path,
            )
        except _adm.AdmissionLeaseUnavailableError:
            try:
                record = _adm.admit_channel_execution(
                    channel_id, config=config, worker_id=worker_id, log=log, now=now,
                    lease_ttl_seconds=lease_ttl_seconds, db_path=db_path,
                )
            except _adm.AdmissionError as exc:
                return CycleResult(channel_id, RuntimeState.BLOCKED, admission_id=record.admission_id,
                                   episode_id=record.episode_id, detail=f"lease lost: {exc}")

    if created:
        return CycleResult(channel_id, RuntimeState.ADMITTED, admission_id=record.admission_id,
                           episode_id=record.episode_id, detail="admitted; enqueued once",
                           created_admission=True)

    # ---- active admission: run (or resume) the pipeline, no publishing ----
    runner = pipeline_runner or _default_pipeline_runner
    try:
        outcome = runner(
            channel_id=channel_id, episode_id=record.episode_id, config=config,
            db_path=db_path, admission=record,
        )
    except Exception as exc:  # noqa: BLE001 -- surface, never swallow
        if log is not None:
            try:
                log.error("autonomous_cycle_pipeline_error", channel_id=channel_id,
                          episode_id=record.episode_id, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
        return CycleResult(channel_id, RuntimeState.WAITING_RETRYABLE, admission_id=record.admission_id,
                           episode_id=record.episode_id, detail=f"pipeline error (retryable): {exc}",
                           invoked_pipeline=True)

    state = _classify_outcome(outcome)
    if state is RuntimeState.READY_TO_PUBLISH:
        _adm.complete_admission(record.admission_id, db_path=db_path)
    elif state is RuntimeState.FAILED:
        _adm.fail_admission(record.admission_id, db_path=db_path)
    return CycleResult(channel_id, state, admission_id=record.admission_id,
                       episode_id=record.episode_id, detail=str(getattr(outcome, "detail", outcome)),
                       invoked_pipeline=True)


def _classify_outcome(outcome) -> RuntimeState:
    status = str(getattr(outcome, "status", outcome) or "").lower()
    if status in ("ready_to_publish", "success", "completed", "ok"):
        return RuntimeState.READY_TO_PUBLISH
    if status in ("blocked", "infeasible", "denied"):
        return RuntimeState.BLOCKED
    if status in ("failed", "error"):
        return RuntimeState.FAILED
    if status in ("paused", "waiting", "retry", "retryable"):
        return RuntimeState.WAITING_RETRYABLE
    return RuntimeState.RUNNING


def _default_pipeline_runner(*, channel_id, episode_id, config, db_path, admission):
    """Real handoff: the frozen pipeline with publishing DISABLED
    (empty platform filter => Publishing Agent reaches ready_to_publish
    but attempts no platform). Reused, not reimplemented."""
    from hypogeum import pipeline as _pipeline
    from hypogeum import topics as _topics

    topic_text = "autonomous"
    if admission.topic_id:
        try:
            row = _topics.get_topic(admission.topic_id, db_path=db_path)
            if row:
                topic_text = row.get("topic_text") or topic_text
        except Exception:  # noqa: BLE001
            pass

    result = _pipeline.run_pipeline(
        topic_text, episode_id, config,
        channel_id=channel_id, db_path=db_path,
        youtube_privacy="private",
        publishing_platform_filter=frozenset(),  # publish NOTHING this turn
    )

    class _Outcome:
        status = "ready_to_publish" if getattr(result, "success", False) else "failed"
        detail = getattr(result, "summary", "")

    return _Outcome()
