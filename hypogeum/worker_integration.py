"""
hypogeum/worker_integration.py — Channel Operations Profile / Backward Plan
gating for the real queue-driven worker path (P3, corrected 2026-07-21).

build_gated_pipeline_executor() is the sole integration point: it returns a
callable matching the pipeline_executor contract hypogeum.queue.process_next()
now accepts (see that function's docstring). Passing this executor into
queue.poll()/process_next() means every claimed run_queue item — whether
autonomously drained by run_worker.py's daemon loop or enqueued for a manual
run — is gated by ChannelOperationsProfile and BackwardPlan before
hypogeum.pipeline.run_pipeline() is ever called. There is no second,
queue-agnostic execution path: claim, topic resolution, complete/fail/
release, and the pipeline call itself all remain exactly where they already
are, in hypogeum/queue.py and hypogeum/pipeline.py.

Reuses, never reimplements:
    hypogeum.channel_config.ChannelOperationsProfile / get_channel_operations_profile
    hypogeum.backward_planner.BackwardPlan / build_backward_plan / save_backward_plan / load_backward_plan
    hypogeum.queue.release() (for a transient/deferred gate block)
    hypogeum.db.get_connection (read-only queries)
    hypogeum.pipeline.run_pipeline() (called exactly once per attempt, unchanged)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from hypogeum.backward_planner import (
    BackwardPlan,
    build_backward_plan,
    load_backward_plan,
    save_backward_plan,
)
from hypogeum.channel_config import (
    SUPPORTED_PLATFORMS,
    _SUPPORTED_PRIVACY_STATES,
    get_channel_operations_profile,
)
from hypogeum.failure_policy import FailurePolicy

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

WORKER_INTEGRATION_VERSION = "2"

_VALID_PLAN_STATUSES = frozenset({"executable", "infeasible", "refused"})


# ---------------------------------------------------------------------------
# Invocation mode
# ---------------------------------------------------------------------------

class InvocationMode(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"

    @classmethod
    def parse(cls, value) -> "InvocationMode":
        """Fail closed: any value other than the two known literals raises ValueError."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"unknown invocation_mode {value!r}; must be one of "
                f"{[m.value for m in cls]}"
            ) from None


# ---------------------------------------------------------------------------
# Exceptions
#
# GatedExecutionDeferred and GatedExecutionRefused are both plain Exception
# subclasses so hypogeum.queue.process_next()'s existing generic
# `except Exception` handler catches either one unchanged (fail() + topic
# release, exactly like any other pipeline failure) — no new queue-level
# exception handling was added. The distinction between them is enforced by
# the executor itself: for a *deferred* (transient) block, the executor
# calls queue.release() on the item BEFORE raising, so the item is already
# back in 'queued' by the time process_next()'s handler runs its own
# fail()/release_topic() calls — those become safe no-ops (release_queue_item/
# fail_queue_item are conditional UPDATEs gated on current status, per
# hypogeum/queue.py's own docs) and the item is genuinely retryable on the
# next poll. For a *refused* (terminal) block, no release() call is made —
# process_next()'s own fail() applies normally, matching existing terminal
# failure semantics.
# ---------------------------------------------------------------------------

class OperationsProfileMissingError(RuntimeError):
    """No ChannelOperationsProfile exists for the requested channel_id."""
    failure_policy = FailurePolicy.TERMINAL  # config/data problem, not timing


class BackwardPlanArtifactError(RuntimeError):
    """An existing backward plan artifact is corrupted or does not match the requested run."""
    failure_policy = FailurePolicy.TERMINAL  # config/data problem, not timing


class WorkerPersistenceError(RuntimeError):
    """Worker attempt metadata could not be durably persisted. Always fail closed on this."""
    # Deliberately left TRANSIENT (default) — a persistence hiccup is
    # plausibly transient; queue-level auto-retry bounds the cost either way.


class GatedExecutionDeferred(RuntimeError):
    """A transient gate (currently: max_concurrent_episodes) blocked this attempt.
    The queue item has already been released back to 'queued' for retry."""
    # TERMINAL for queue-level auto-retry purposes specifically — not because
    # this is unretryable, but because it already self-manages its own retry
    # (release() called before raising, see module docstring above). Auto-retry
    # creating a second, separate retry item for the same episode_id would
    # race against this class's own already-released queue item.
    failure_policy = FailurePolicy.TERMINAL


class GatedExecutionRefused(RuntimeError):
    """A terminal gate (profile/plan) blocked this attempt. The queue item
    will be marked 'failed' by process_next()'s existing exception handling."""
    failure_policy = FailurePolicy.TERMINAL


# ---------------------------------------------------------------------------
# Result shape — one immutable record per attempt (see save_worker_attempt).
# ---------------------------------------------------------------------------

@dataclass
class StageScheduleRisk:
    stage_name: str
    planned_start_at: Optional[str]
    planned_finish_at: Optional[str]
    actual_started_at: Optional[str]
    actual_finished_at: Optional[str]
    started_late_by_seconds: Optional[int]
    finished_late_by_seconds: Optional[int]
    deadline_status: str  # "on_time" | "at_risk" | "late" | "not_applicable"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkerAttempt:
    worker_run_id: str
    channel_id: str
    episode_id: str
    invocation_mode: str
    plan_id: Optional[str]
    plan_status: Optional[str]
    target_publish_at: Optional[str]
    blocked_reason: Optional[str]
    schedule_status: str  # "on_time" | "at_risk" | "late" | "not_applicable" | "unknown"
    started_at: Optional[str]
    finished_at: Optional[str]
    eligible_platforms: list
    excluded_platforms: dict  # platform -> reason
    required_lead_time_seconds: Optional[int] = None
    available_lead_time_seconds: Optional[int] = None
    earliest_feasible_publish_at: Optional[str] = None
    stage_schedule_risk: list = field(default_factory=list)  # list[dict]
    worker_integration_version: str = WORKER_INTEGRATION_VERSION
    success: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkerAttempt":
        return cls(**d)


# ---------------------------------------------------------------------------
# Operations profile gating
# ---------------------------------------------------------------------------

def load_operations_profile_or_fail(channel_id: str, db_path=None):
    """Fail closed: raises OperationsProfileMissingError when no profile row exists.
    Any other read/corruption failure already raises RuntimeError from
    get_channel_operations_profile() itself — propagated unchanged."""
    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        raise OperationsProfileMissingError(
            f"no ChannelOperationsProfile exists for channel_id={channel_id!r} — "
            f"cannot gate a worker run without one"
        )
    return profile


def check_invocation_gates(profile, invocation_mode: InvocationMode) -> Optional[str]:
    """Returns None if execution is authorized, else a human-readable blocked_reason."""
    if not profile.operator_enabled:
        return "operator_enabled is False — operator has not authorized this channel to run"
    if not profile.production_enabled:
        return "production_enabled is False — production is disabled for this channel"
    if invocation_mode == InvocationMode.SCHEDULED and not profile.unattended_operation_enabled:
        return (
            "unattended_operation_enabled is False — scheduled/unattended invocation "
            "is not authorized for this channel"
        )
    return None


def resolve_publishing_platforms(profile) -> tuple:
    """
    Returns (eligible_platforms: list[str], excluded_platforms: dict[str, str]).

    A platform is eligible only when both enabled AND not blocked. No platform
    is ever treated as enabled simply because credentials exist — this reads
    only ChannelOperationsProfile.enabled_platforms / blocked_platform_reasons.
    """
    enabled = profile.enabled_platforms or {}
    blocked = profile.blocked_platform_reasons or {}
    eligible: list = []
    excluded: dict = {}
    for platform in SUPPORTED_PLATFORMS:
        if platform in blocked:
            excluded[platform] = blocked[platform]
        elif not enabled.get(platform):
            excluded[platform] = "not enabled for this channel"
        else:
            eligible.append(platform)
    return eligible, excluded


# ---------------------------------------------------------------------------
# Concurrency gate — v1: count real 'running' pipeline runs for this channel
# in the runs table (populated by hypogeum.run_registry.register_run(),
# which stamps channel_id — see hypogeum/pipeline.py's run registration
# step). Not a general resource scheduler: a single read-count against
# existing state, sufficient for the current single-worker-process
# deployment model. A second concurrent worker process has a narrow race
# window between this count and the next run's registration — acceptable
# for v1 per the task's explicit "do not build a general resource
# scheduler" instruction.
# ---------------------------------------------------------------------------

def count_active_runs_for_channel(channel_id: str, db_path=None) -> int:
    from hypogeum.db import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE channel_id = ? AND status = 'running'",
            (channel_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Target lookup — reuses the SAME episode_topics.target_publish_at column
# hypogeum/pipeline.py's own _get_episode_publish_at() already reads to
# schedule the real YouTube publishAt call. This is not an invented target:
# it is whatever hypogeum.dashboard_control.enqueue_topic() (or an approved
# discovery candidate) already wrote for this episode before the worker ever
# claimed it. None when no such row exists — never fabricated.
#
# Keyed by episode_id — see the identity contract documented at the write
# site, hypogeum/queue.py::process_next() Step 5 (topic_id claimed ->
# episode_id assigned -> episode_topics.episode_id persisted ->
# target_publish_at resolved here by episode_id). This lookup only finds a
# scheduler-derived target_publish_at (written keyed by topic_id via
# hypogeum.topics.set_topic_target_publish_at) once that persistence has
# happened; do not read here by episode_id while writing elsewhere by
# topic_id without that mapping existing first.
# ---------------------------------------------------------------------------

def lookup_episode_target_publish_at(episode_id: str, db_path=None) -> Optional[datetime]:
    from hypogeum.db import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT target_publish_at FROM episode_topics "
            "WHERE episode_id = ? AND target_publish_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or not row["target_publish_at"]:
        return None
    try:
        return datetime.fromisoformat(row["target_publish_at"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Backward plan resolution
# ---------------------------------------------------------------------------

def _validate_plan_shape(plan: BackwardPlan, channel_id: str) -> None:
    if plan.plan_status not in _VALID_PLAN_STATUSES:
        raise BackwardPlanArtifactError(
            f"backward plan artifact has unknown plan_status {plan.plan_status!r}"
        )
    if plan.channel_id != channel_id:
        raise BackwardPlanArtifactError(
            f"backward plan artifact channel_id={plan.channel_id!r} does not match "
            f"requested channel_id={channel_id!r}"
        )
    if not plan.plan_id:
        raise BackwardPlanArtifactError("backward plan artifact is missing plan_id")
    try:
        datetime.fromisoformat(plan.target_publish_at)
    except Exception as exc:
        raise BackwardPlanArtifactError(
            f"backward plan artifact has an invalid target_publish_at: {exc}"
        ) from exc


def resolve_backward_plan(
    channel_id: str,
    episode_id: str,
    invocation_mode: InvocationMode,
    profile,
    target_publish_at: Optional[datetime] = None,
    generated_at: Optional[datetime] = None,
    workload: Optional[dict] = None,
    project_root: Optional[Path] = None,
) -> tuple:
    """
    Returns (plan, blocked_reason).

    plan is None only when no plan applies at all (no existing artifact, no
    target_publish_at — the pre-existing unscheduled flow this correction
    still honors for every queue item that has no production deadline
    recorded on episode_topics). blocked_reason is set whenever production
    must not begin (refused/infeasible plan, or scheduled mode missing both
    a plan and a target).

    Rules (never violated):
      - An existing plan artifact always wins over building a new one, so
        plan_id is preserved across resumes without ever being rebuilt.
      - Corrupted or channel-mismatched artifacts raise BackwardPlanArtifactError
        rather than being silently accepted or discarded.
      - Scheduled-invocation items with neither an artifact nor a target
        fail closed (see build_gated_pipeline_executor's invocation_mode
        derivation — this only applies to items that actually came through
        hypogeum.scheduler, not every autonomous queue item).
    """
    try:
        existing = load_backward_plan(episode_id, project_root=project_root)
    except Exception as exc:
        raise BackwardPlanArtifactError(
            f"corrupted backward plan artifact for episode_id={episode_id!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if existing is not None:
        _validate_plan_shape(existing, channel_id)
        plan = existing
    elif target_publish_at is not None:
        if generated_at is None:
            generated_at = datetime.now(timezone.utc)
        plan = build_backward_plan(
            channel_id, target_publish_at, profile, generated_at, workload=workload,
        )
        save_backward_plan(plan, episode_id, project_root=project_root)
    else:
        if invocation_mode == InvocationMode.SCHEDULED:
            return None, (
                "scheduled invocation requires an existing backward plan artifact "
                "or an explicit target_publish_at — neither was supplied"
            )
        return None, None

    if plan.plan_status == "refused":
        return plan, f"backward plan refused: {plan.refusal_reason}"
    if plan.plan_status == "infeasible":
        return plan, (
            f"backward plan infeasible: required_lead_time_seconds="
            f"{plan.required_lead_time_seconds} available_lead_time_seconds="
            f"{plan.available_lead_time_seconds} "
            f"earliest_feasible_publish_at={plan.earliest_feasible_publish_at}"
        )
    return plan, None


# ---------------------------------------------------------------------------
# Stage progress / schedule-risk — read-only against stage_executions.
# ---------------------------------------------------------------------------

def get_latest_stage_rows(episode_id: str, db_path=None) -> dict:
    """Returns {stage_name: sqlite3.Row} — the most recent row per stage_name
    for this episode (a resumed episode may have several rows per stage across
    retries; only the latest reflects current truth)."""
    from hypogeum.db import get_connection

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT stage_name, started_at, completed_at, status
            FROM stage_executions
            WHERE episode_id = ?
            ORDER BY started_at ASC
            """,
            (episode_id,),
        ).fetchall()
    finally:
        conn.close()

    latest: dict = {}
    for row in rows:
        latest[row["stage_name"]] = row  # later rows overwrite earlier ones (ASC order)
    return latest


def compute_stage_schedule_risk(
    plan: Optional[BackwardPlan],
    episode_id: str,
    db_path=None,
    evaluated_at: Optional[datetime] = None,
) -> list:
    """
    Compares each planned stage's timing against the latest stage_executions
    row for that stage. Empty when there is no plan to compare against.
    Never compresses or mutates plan durations — read-only observation.
    """
    if plan is None or not plan.stages:
        return []

    if evaluated_at is None:
        evaluated_at = datetime.now(timezone.utc)

    latest_by_stage = get_latest_stage_rows(episode_id, db_path=db_path)

    results = []
    for sp in plan.stages:
        row = latest_by_stage.get(sp.stage_name)
        planned_start = datetime.fromisoformat(sp.planned_start_at)
        planned_finish = datetime.fromisoformat(sp.planned_finish_at)

        actual_started_at = row["started_at"] if row else None
        actual_finished_at = row["completed_at"] if row else None

        started_late = None
        finished_late = None
        status = "not_applicable"

        if actual_started_at:
            actual_start_dt = datetime.fromisoformat(actual_started_at)
            started_late = int((actual_start_dt - planned_start).total_seconds())

            if actual_finished_at:
                actual_finish_dt = datetime.fromisoformat(actual_finished_at)
                finished_late = int((actual_finish_dt - planned_finish).total_seconds())
                status = "late" if finished_late > 0 else "on_time"
            else:
                status = "at_risk" if evaluated_at > planned_finish else "on_time"

        results.append(StageScheduleRisk(
            stage_name=sp.stage_name,
            planned_start_at=sp.planned_start_at,
            planned_finish_at=sp.planned_finish_at,
            actual_started_at=actual_started_at,
            actual_finished_at=actual_finished_at,
            started_late_by_seconds=started_late,
            finished_late_by_seconds=finished_late,
            deadline_status=status,
        ).to_dict())

    return results


def _schedule_status_from_risk(plan: Optional[BackwardPlan], stage_schedule_risk: list) -> str:
    if plan is None:
        return "not_applicable"
    statuses = [s["deadline_status"] for s in stage_schedule_risk]
    if any(s == "late" for s in statuses):
        return "late"
    if any(s == "at_risk" for s in statuses):
        return "at_risk"
    if statuses:
        return "on_time"
    return "not_applicable"


# ---------------------------------------------------------------------------
# Worker attempt persistence — one immutable file per attempt plus a "latest"
# pointer, so resuming an episode never overwrites prior attempt history.
# Mirrors backward_planner's own JSON-artifact convention (no new DB table).
# Fails closed: never swallows a write error.
# ---------------------------------------------------------------------------

def _episode_dir(episode_id: str, project_root: Optional[Path]) -> Path:
    root = project_root if project_root is not None else _PROJECT_ROOT
    return root / ".hypogeum-assets" / "episodes" / episode_id


def _worker_attempt_path(episode_id: str, worker_run_id: str, project_root: Optional[Path]) -> Path:
    return _episode_dir(episode_id, project_root) / "worker_attempts" / f"{worker_run_id}.json"


def _worker_run_latest_path(episode_id: str, project_root: Optional[Path]) -> Path:
    return _episode_dir(episode_id, project_root) / "worker_run_latest.json"


def save_worker_attempt(attempt: WorkerAttempt, project_root: Optional[Path] = None) -> Path:
    """
    Writes {episode_dir}/worker_attempts/{worker_run_id}.json (one immutable
    file per attempt — never overwritten) and updates
    {episode_dir}/worker_run_latest.json to point at it. Raises
    WorkerPersistenceError on any write failure — never swallowed.
    """
    attempt_path = _worker_attempt_path(attempt.episode_id, attempt.worker_run_id, project_root)
    latest_path = _worker_run_latest_path(attempt.episode_id, project_root)
    try:
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_path.write_text(json.dumps(attempt.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        latest_path.write_text(json.dumps(attempt.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        raise WorkerPersistenceError(
            f"failed to persist worker attempt for channel_id={attempt.channel_id!r} "
            f"episode_id={attempt.episode_id!r} worker_run_id={attempt.worker_run_id!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return attempt_path


def load_latest_worker_attempt(episode_id: str, project_root: Optional[Path] = None) -> Optional[WorkerAttempt]:
    path = _worker_run_latest_path(episode_id, project_root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return WorkerAttempt.from_dict(data)


def list_worker_attempts(episode_id: str, project_root: Optional[Path] = None) -> list:
    """Returns all persisted attempts for this episode, oldest first (by started_at)."""
    attempts_dir = _episode_dir(episode_id, project_root) / "worker_attempts"
    if not attempts_dir.exists():
        return []
    records = [
        WorkerAttempt.from_dict(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(attempts_dir.glob("*.json"))
    ]
    records.sort(key=lambda a: a.started_at or "")
    return records


# ---------------------------------------------------------------------------
# The canonical gated executor — the sole integration point.
# ---------------------------------------------------------------------------

def build_gated_pipeline_executor(
    db_path=None, project_root: Optional[Path] = None, bounded_run_id: Optional[str] = None,
):
    """
    Returns a callable matching hypogeum.queue.process_next()'s
    pipeline_executor contract:
        executor(*, topic_input, episode_id, config, channel_id,
                 cancel_check, queue_id, worker_id)

    Pass the result as queue.poll(..., pipeline_executor=...) or
    queue.process_next(..., pipeline_executor=...) to gate every claimed
    queue item with ChannelOperationsProfile + BackwardPlan before
    hypogeum.pipeline.run_pipeline() is invoked. db_path/project_root are
    captured once here (closure) — process_next() itself never needs to
    know about either.

    bounded_run_id: Optional. None (default, every existing caller)
        preserves prior behavior exactly. When given, this executor reads
        hypogeum.db.bounded_runs.publishing_allowed for that bounded_run_id
        and, when it is falsy (0, or the row cannot be read at all — fail
        closed rather than assume authorization), forces the publishing
        platform filter to an active-but-empty set (frozenset()) regardless
        of the channel's own ChannelOperationsProfile — CORE INVARIANTS #13:
        publishing_allowed=0 is enforced here, at the actual execution path,
        before hypogeum.pipeline.run_pipeline() is ever called, so zero
        publishing platform calls are made. This does not gate whether the
        claim itself is authorized — that is queue.claim_next_bounded()'s
        job — only whether the run this executor is about to launch may
        publish.

    Gate order for a channel-scoped item (channel_id is not None — matches
    process_next()'s own CALOG check, which is likewise skipped for
    factory-level/unscoped items):
      1. Load ChannelOperationsProfile — fail closed if missing/corrupt.
      2. invocation_mode is derived PER ITEM from whether it came through
         hypogeum.scheduler (run_queue.scheduled_run_id is set => "scheduled")
         or not (manually enqueued / topic-backlog-driven => "manual"). This
         is not a caller-supplied flag — it is read from the claimed item
         itself via queue.get_item(queue_id), so unattended_operation_enabled
         is only required for genuinely scheduler-originated work, and every
         other existing queue path (today's only real traffic) continues to
         run under the manual/supervised gate contract.
      3. operator_enabled / production_enabled (always) and
         unattended_operation_enabled (scheduled only).
      4. max_concurrent_episodes — count_active_runs_for_channel(); over the
         limit raises GatedExecutionDeferred after releasing the queue item
         for retry (transient block, never a hard failure).
      5. BackwardPlan: load existing artifact for episode_id, or build one
         from episode_topics.target_publish_at if present (never invented
         from cadence) — resolve_backward_plan() never rebuilds an existing
         artifact, so plan_id survives resume. Refused/infeasible/corrupted
         plans raise GatedExecutionRefused (terminal).
      6. Publishing platform filter — resolve_publishing_platforms(), forced
         to empty when publishing_enabled is False — passed to
         hypogeum.pipeline.run_pipeline()'s publishing_platform_filter
         parameter, which reaches hypogeum.agents.publishing_agent.run_publishing_agent()'s
         enabled_platforms parameter. Disabled/blocked platforms never reach
         a provider call (see that function's docstring).
      7. youtube_privacy — run_pipeline() defaults this to "public" and this
         executor never previously passed it, so every autonomous run
         silently uploaded public. Resolved from the already-loaded
         profile.default_privacy_state and passed explicitly;
         GatedExecutionRefused (terminal) if that value is missing or not
         one of channel_config._SUPPORTED_PRIVACY_STATES — never falls
         through to run_pipeline()'s public default.

    Every gate outcome (blocked or not) is persisted via save_worker_attempt()
    — one immutable file per attempt plus a "latest" pointer (see
    list_worker_attempts() / load_latest_worker_attempt()) — before either
    raising or calling run_pipeline(), so state is truthful even if this
    process is interrupted mid-run. save_worker_attempt() itself fails closed
    (WorkerPersistenceError propagates, never swallowed).
    """
    def _executor(
        *, topic_input, episode_id, config, channel_id,
        cancel_check=None, queue_id=None, worker_id=None,
    ) -> None:
        from hypogeum.pipeline import run_pipeline

        # Bounded-run publishing boundary — resolved once per call, applied
        # in both branches below. Fail closed: an unreadable/missing
        # bounded_runs row is treated the same as publishing_allowed=0.
        bounded_publishing_forced_off = False
        if bounded_run_id is not None:
            from hypogeum.db import get_bounded_run, get_connection as _get_connection

            _conn = _get_connection(db_path)
            try:
                _bounded_run = get_bounded_run(_conn, bounded_run_id)
            finally:
                _conn.close()
            bounded_publishing_forced_off = (
                _bounded_run is None or not _bounded_run.get("publishing_allowed")
            )

        if channel_id is None:
            # No channel scope — nothing to gate (matches process_next()'s
            # own CALOG check, which is also conditional on channel_id).
            # publishing_platform_filter=None means "no filter" (every
            # platform enabled) downstream — an empty frozenset() is what
            # actually blocks every platform, so bounded-forced-off must
            # pass that, never None, here.
            run_pipeline(
                topic_input=topic_input, episode_id=episode_id, config=config,
                channel_id=channel_id, db_path=db_path, cancel_check=cancel_check,
                publishing_platform_filter=frozenset() if bounded_publishing_forced_off else None,
            )
            return

        from hypogeum.queue import get_item, release

        row = get_item(queue_id, db_path=db_path) if queue_id else None
        scheduled_run_id = row.get("scheduled_run_id") if row else None
        mode = InvocationMode.SCHEDULED if scheduled_run_id else InvocationMode.MANUAL

        attempt = WorkerAttempt(
            worker_run_id=str(uuid.uuid4()),
            channel_id=channel_id,
            episode_id=episode_id,
            invocation_mode=mode.value,
            plan_id=None,
            plan_status=None,
            target_publish_at=None,
            blocked_reason=None,
            schedule_status="unknown",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            eligible_platforms=[],
            excluded_platforms={},
        )

        def _refuse(reason: str) -> None:
            attempt.blocked_reason = reason
            attempt.finished_at = datetime.now(timezone.utc).isoformat()
            save_worker_attempt(attempt, project_root=project_root)
            raise GatedExecutionRefused(reason)

        def _defer(reason: str) -> None:
            attempt.blocked_reason = reason
            attempt.finished_at = datetime.now(timezone.utc).isoformat()
            save_worker_attempt(attempt, project_root=project_root)
            if queue_id and worker_id:
                release(queue_id, worker_id, db_path=db_path)
            raise GatedExecutionDeferred(reason)

        try:
            profile = load_operations_profile_or_fail(channel_id, db_path=db_path)
        except OperationsProfileMissingError as exc:
            _refuse(str(exc))
            return  # unreachable — _refuse always raises

        gate_reason = check_invocation_gates(profile, mode)
        if gate_reason is not None:
            _refuse(gate_reason)
            return

        active = count_active_runs_for_channel(channel_id, db_path=db_path)
        if active >= max(1, profile.max_concurrent_episodes):
            _defer(
                f"max_concurrent_episodes reached ({active}/{profile.max_concurrent_episodes}) "
                f"for channel_id={channel_id!r}"
            )
            return

        target_publish_at = lookup_episode_target_publish_at(episode_id, db_path=db_path)
        try:
            plan, plan_blocked_reason = resolve_backward_plan(
                channel_id=channel_id, episode_id=episode_id, invocation_mode=mode,
                profile=profile, target_publish_at=target_publish_at,
                generated_at=datetime.now(timezone.utc), project_root=project_root,
            )
        except (BackwardPlanArtifactError, ValueError) as exc:
            _refuse(str(exc))
            return

        if plan is not None:
            attempt.plan_id = plan.plan_id
            attempt.plan_status = plan.plan_status
            attempt.target_publish_at = plan.target_publish_at
            attempt.required_lead_time_seconds = plan.required_lead_time_seconds
            attempt.available_lead_time_seconds = plan.available_lead_time_seconds
            attempt.earliest_feasible_publish_at = plan.earliest_feasible_publish_at

        if plan_blocked_reason is not None:
            _refuse(plan_blocked_reason)
            return

        eligible_platforms, excluded_platforms = resolve_publishing_platforms(profile)
        if not profile.publishing_enabled:
            excluded_platforms = {p: "publishing_enabled is False for this channel" for p in eligible_platforms}
            eligible_platforms = []
        if bounded_publishing_forced_off:
            for _p in eligible_platforms:
                excluded_platforms[_p] = (
                    f"publishing_allowed is False for bounded_run_id={bounded_run_id!r}"
                )
            eligible_platforms = []
        attempt.eligible_platforms = eligible_platforms
        attempt.excluded_platforms = excluded_platforms

        # youtube_privacy: run_pipeline() itself defaults to "public", and
        # this executor is the only place that gates every autonomous
        # queue-driven run before it reaches run_pipeline() — so this is
        # where the channel's actual privacy intent must be resolved and
        # passed explicitly. ChannelOperationsProfile.default_privacy_state
        # is the canonical source (already loaded above as `profile`).
        # Fail closed: an unresolved/invalid value refuses the run rather
        # than silently falling through to run_pipeline()'s public default.
        resolved_privacy = getattr(profile, "default_privacy_state", None)
        if resolved_privacy not in _SUPPORTED_PRIVACY_STATES:
            _refuse(
                f"cannot resolve a valid youtube_privacy from "
                f"ChannelOperationsProfile.default_privacy_state="
                f"{resolved_privacy!r} for channel_id={channel_id!r} — "
                f"refusing rather than defaulting to a public upload"
            )
            return

        save_worker_attempt(attempt, project_root=project_root)

        try:
            run_pipeline(
                topic_input=topic_input, episode_id=episode_id, config=config,
                channel_id=channel_id, db_path=db_path, cancel_check=cancel_check,
                publishing_platform_filter=frozenset(eligible_platforms),
                youtube_privacy=resolved_privacy,
            )
            attempt.success = True
        finally:
            attempt.finished_at = datetime.now(timezone.utc).isoformat()
            progress_risk = compute_stage_schedule_risk(plan, episode_id, db_path=db_path)
            attempt.stage_schedule_risk = progress_risk
            attempt.schedule_status = _schedule_status_from_risk(plan, progress_risk)
            save_worker_attempt(attempt, project_root=project_root)

    return _executor
