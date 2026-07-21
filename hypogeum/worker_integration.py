"""
hypogeum/worker_integration.py — run_worker.py <-> ChannelOperationsProfile
<-> BackwardPlan integration helpers.

Extracted from run_worker.py into its own module because it would otherwise
be materially harder to unit-test in isolation (gate logic, plan
loading/validation, platform filtering, and schedule-risk computation are
all pure-ish functions over existing models, independent of the daemon
loop). This module does not implement Scheduler behavior and never claims
from run_queue — see run_worker.py::run_gated_episode() for the actual
integration point that calls hypogeum.pipeline.run_pipeline().

Reuses, never reimplements:
    hypogeum.channel_config.ChannelOperationsProfile / get_channel_operations_profile
    hypogeum.backward_planner.BackwardPlan / build_backward_plan / save_backward_plan / load_backward_plan
    hypogeum.db.get_connection (read-only queries against stage_executions)
"""

from __future__ import annotations

import json
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
from hypogeum.channel_config import SUPPORTED_PLATFORMS, get_channel_operations_profile

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Independent of hypogeum.backward_planner's own module version — this is the
# WORKER-side gating contract version, recorded on every WorkerRunResult so a
# future Scheduler/Dashboard can tell which gating contract produced a run.
WORKER_INTEGRATION_VERSION = "1"

_VALID_PLAN_STATUSES = frozenset({"executable", "infeasible", "refused"})
_TERMINAL_STAGE_STATUSES = ("completed", "failed", "skipped")


# ---------------------------------------------------------------------------
# Invocation mode — smallest explicit mechanism per the task: an enum with
# exactly two known values. Anything else fails closed (InvocationMode.parse).
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
# ---------------------------------------------------------------------------

class OperationsProfileMissingError(RuntimeError):
    """No ChannelOperationsProfile exists for the requested channel_id."""


class BackwardPlanArtifactError(RuntimeError):
    """An existing backward plan artifact is corrupted or does not match the requested run."""


class WorkerPersistenceError(RuntimeError):
    """Worker run metadata could not be durably persisted. Always fail closed on this."""


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class WorkerRunResult:
    worker_run_id: str
    channel_id: str
    episode_id: str
    invocation_mode: str
    plan_id: Optional[str]
    plan_status: Optional[str]
    target_publish_at: Optional[str]
    current_stage: Optional[str]
    completed_stages: list
    skipped_stages: list
    failed_stage: Optional[str]
    blocked_reason: Optional[str]
    schedule_status: str  # "on_time" | "at_risk" | "late" | "not_applicable" | "unknown"
    started_at: Optional[str]
    finished_at: Optional[str]
    checkpoint_status: str  # "fresh" | "resumed" | "not_applicable"
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
    def from_dict(cls, d: dict) -> "WorkerRunResult":
        return cls(**d)


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

    plan is None only when no plan applies at all — manual mode, no existing
    artifact, no target_publish_at supplied (the pre-existing unscheduled
    supervised flow). blocked_reason is set whenever production must not
    begin (refused/infeasible plan, or scheduled mode missing both a plan
    and a target).

    Rules (never violated):
      - An existing plan artifact always wins over building a new one, so
        plan_id is preserved across resumes without ever being rebuilt.
      - Corrupted or channel-mismatched artifacts raise BackwardPlanArtifactError
        rather than being silently accepted or discarded.
      - Scheduled mode with neither an artifact nor a target fails closed.
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
        return None, None  # manual, unscheduled supervised flow — no plan enforced

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
# Never writes, never invents progress that isn't in the DB.
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
    row for that stage. Returns list[dict] (StageScheduleRisk.to_dict()).
    Empty when there is no plan to compare against — never invents a plan.

    Never compresses or mutates plan durations — this is a read-only
    observation layer over the plan and the DB.
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


def summarize_stage_progress(
    plan: Optional[BackwardPlan],
    episode_id: str,
    db_path=None,
    topic_selected: bool = False,
    evaluated_at: Optional[datetime] = None,
) -> dict:
    """
    Single read of stage_executions, used to populate current_stage,
    completed_stages, skipped_stages, failed_stage, stage_schedule_risk, and
    schedule_status truthfully — never asserting progress beyond what the DB
    actually records.

    topic_selection is synthetic (see hypogeum.backward_planner's module
    docstring) — it is never a stage_executions row. When a topic has
    genuinely been selected (topic_selected=True, i.e. topic_input was
    resolved — from an explicit caller value or the topic backlog — before
    this call), it is reported as completed using that existing evidence,
    without inventing any new execution for it.
    """
    latest_by_stage = get_latest_stage_rows(episode_id, db_path=db_path)

    completed_stages = ["topic_selection"] if topic_selected else []
    skipped_stages: list = []
    failed_stage: Optional[str] = None
    current_stage: Optional[str] = None

    # Preserve chronological order via started_at across all known stages.
    ordered = sorted(latest_by_stage.values(), key=lambda r: r["started_at"])
    for row in ordered:
        if row["status"] == "completed":
            completed_stages.append(row["stage_name"])
        elif row["status"] == "skipped":
            skipped_stages.append(row["stage_name"])
        elif row["status"] == "failed":
            failed_stage = row["stage_name"]
        elif row["status"] == "running":
            current_stage = row["stage_name"]

    if current_stage is None and ordered:
        current_stage = ordered[-1]["stage_name"]
    elif current_stage is None and topic_selected:
        current_stage = "topic_selection"

    stage_schedule_risk = compute_stage_schedule_risk(
        plan, episode_id, db_path=db_path, evaluated_at=evaluated_at,
    )

    if plan is None:
        schedule_status = "not_applicable"
    else:
        statuses = [s["deadline_status"] for s in stage_schedule_risk]
        if any(s == "late" for s in statuses):
            schedule_status = "late"
        elif any(s == "at_risk" for s in statuses):
            schedule_status = "at_risk"
        elif statuses:
            schedule_status = "on_time"
        else:
            schedule_status = "not_applicable"

    return {
        "completed_stages": completed_stages,
        "skipped_stages": skipped_stages,
        "failed_stage": failed_stage,
        "current_stage": current_stage,
        "stage_schedule_risk": stage_schedule_risk,
        "schedule_status": schedule_status,
    }


# ---------------------------------------------------------------------------
# Worker run result persistence — mirrors backward_planner's own JSON-artifact
# convention (no new DB table). Fails closed: never swallows a write error.
# ---------------------------------------------------------------------------

def _worker_run_artifact_path(episode_id: str, project_root: Optional[Path]) -> Path:
    root = project_root if project_root is not None else _PROJECT_ROOT
    return root / ".hypogeum-assets" / "episodes" / episode_id / f"worker_run_{episode_id}.json"


def save_worker_run_result(result: WorkerRunResult, project_root: Optional[Path] = None) -> Path:
    path = _worker_run_artifact_path(result.episode_id, project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        raise WorkerPersistenceError(
            f"failed to persist worker run result for channel_id={result.channel_id!r} "
            f"episode_id={result.episode_id!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return path


def load_worker_run_result(episode_id: str, project_root: Optional[Path] = None) -> Optional[WorkerRunResult]:
    path = _worker_run_artifact_path(episode_id, project_root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return WorkerRunResult.from_dict(data)
