"""
hypogeum/backward_planner.py — Deterministic Backward Production Planner

Converts a channel's publishing target into an actionable, stage-by-stage
production timeline for one episode, working backward from
target_publish_at through the real pipeline stage order.

This is NOT a scheduler and NOT a new workflow engine. It is a pure,
deterministic calculation consumed later by run_worker.py and
hypogeum/scheduler.py (that integration is a separate, later task — this
module has no dependency on either).

Relationship to existing code:
  hypogeum.dashboard_control.enqueue_topic() already writes a much simpler
  production_deadline_at = target_publish_at - 24h onto episode_topics. That
  naive single-buffer calculation is not touched or replaced here — this
  module is a superset capability for a future integration step to adopt,
  not a duplicate scheduler.

Stage order (matches hypogeum.pipeline.run_pipeline()'s real, currently
sequential _record_stage_start() checkpoint names exactly, except
"topic_selection" — see PIPELINE_STAGE_ORDER docstring below):
  topic_selection (synthetic) -> startup_checks -> executive_producer ->
  research_director -> narrative_architect -> script_engine ->
  finance_agent_authorization -> thumbnail_strategy ->
  visual_package_director -> thumbnail_director -> audio_agent ->
  assembly_agent -> asset_archive -> qa_agent -> compliance_agent ->
  publishing_agent

Public API:
    build_backward_plan(channel_id, target_publish_at, operations_profile,
                        generated_at, stage_durations=None,
                        safety_buffer_seconds=3600) -> BackwardPlan
    save_backward_plan(plan, episode_id, project_root=None) -> Path
    load_backward_plan(episode_id, project_root=None) -> Optional[BackwardPlan]
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hypogeum.channel_config import SUPPORTED_PLATFORMS

# ---------------------------------------------------------------------------
# Stage order
#
# "topic_selection" is NOT a hypogeum.pipeline.run_pipeline() checkpoint —
# discovery/ranking/candidate-selection runs as a separate, asynchronous
# queue-refill cycle (hypogeum.topic_discovery), not a per-episode pipeline
# stage. It is included here, clearly labeled, because the whole purpose of
# backward planning is to also surface *when* a topic must already be
# selected/queued for the target publish date to be achievable. Every other
# name below is copied verbatim from the real _record_stage_start() calls in
# hypogeum/pipeline.py::run_pipeline() — no inactive/invented checkpoint
# names. archive_agent and finance_agent_ledger are real but POST-publish
# (they run after publishing_agent) and are excluded from this pre-publish
# lead-time calculation on purpose.
# ---------------------------------------------------------------------------

PIPELINE_STAGE_ORDER: tuple[str, ...] = (
    "topic_selection",
    "startup_checks",
    "executive_producer",
    "research_director",
    "narrative_architect",
    "script_engine",
    "finance_agent_authorization",
    "thumbnail_strategy",
    "visual_package_director",
    "thumbnail_director",
    "audio_agent",
    "assembly_agent",
    "asset_archive",
    "qa_agent",
    "compliance_agent",
    "publishing_agent",
)

_PUBLISHING_STAGE = "publishing_agent"

# ---------------------------------------------------------------------------
# Default duration estimates — seconds. Smallest transparent v1 mechanism:
# named constants, one local structure, no benchmarking/ML, no secrets.
# These are deliberately rough, documented defaults an operator can override
# per call via the stage_durations parameter as real data becomes available.
# publishing_agent has no fixed default here — its duration is computed from
# PUBLISHING_PREP_DURATION_SECONDS + only the channel's currently enabled
# platforms (see _resolve_publishing_duration), unless explicitly overridden.
# ---------------------------------------------------------------------------

DEFAULT_STAGE_DURATIONS_SECONDS: dict[str, int] = {
    "topic_selection": 300,               # 5 min  — candidate review/selection
    "startup_checks": 30,                 # 30 sec — provider reachability check
    "executive_producer": 180,            # 3 min  — topic scoring LLM call
    "research_director": 900,             # 15 min — Perplexity research queries
    "narrative_architect": 300,           # 5 min  — story structure LLM call
    "script_engine": 600,                 # 10 min — narration + visual direction LLM call
    "finance_agent_authorization": 30,    # 30 sec — budget gate check
    "thumbnail_strategy": 180,            # 3 min  — CTR concept LLM call
    "visual_package_director": 5400,      # 90 min — scene planning + MUAPI video generation
    "thumbnail_director": 900,            # 15 min — thumbnail image generation + compositing
    "audio_agent": 600,                   # 10 min — ElevenLabs narration + music
    "assembly_agent": 1200,               # 20 min — FFmpeg render
    "asset_archive": 60,                  # 1 min  — asset cataloging
    "qa_agent": 300,                      # 5 min  — pre-publish QA checks
    "compliance_agent": 180,              # 3 min  — rights/compliance checks
}

# publishing_agent duration = prep (once) + sum of per-enabled-platform time.
PUBLISHING_PREP_DURATION_SECONDS = 300  # 5 min — metadata/description building, common to all platforms

PER_PLATFORM_UPLOAD_DURATION_SECONDS: dict[str, int] = {
    "youtube": 600,     # 10 min — main + shorts upload, captions, thumbnail
    "facebook": 300,    # 5 min
    "tiktok": 300,      # 5 min
    "instagram": 300,   # 5 min
    "email": 60,        # 1 min
}

# Flat per-stage padding: a simple, deterministic, documented fraction —
# not a statistical/ML estimate.
_PER_STAGE_SAFETY_BUFFER_FRACTION = 0.10

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Typed plan
# ---------------------------------------------------------------------------

@dataclass
class StagePlan:
    stage_name: str
    sequence_index: int
    planned_start_at: str
    planned_finish_at: str
    estimated_duration_seconds: int
    safety_buffer_seconds: int
    retry_allowance_seconds: int
    hard_deadline_at: str
    depends_on: Optional[str]
    critical: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StagePlan":
        return cls(**d)


@dataclass
class BackwardPlan:
    """
    plan_status:
        "executable" — production_start_at is not in the past; the plan can
                        be handed to a worker as-is.
        "infeasible" — the target publish time does not leave enough lead
                        time from generated_at. required_lead_time_seconds,
                        available_lead_time_seconds, earliest_feasible_publish_at,
                        and blocking_stages are populated. Durations are
                        NEVER silently compressed to force feasibility.
        "refused"    — operator_enabled or production_enabled is False.
                        refusal_reason explains which. stages is empty.
    """
    plan_id: str
    channel_id: str
    target_publish_at: str
    production_start_at: str
    timezone: str
    generated_at: str
    plan_status: str
    stages: list
    max_concurrent_episodes: int
    production_complete_at: Optional[str] = None
    required_lead_time_seconds: Optional[int] = None
    available_lead_time_seconds: Optional[int] = None
    earliest_feasible_publish_at: Optional[str] = None
    blocking_stages: list = field(default_factory=list)
    refusal_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackwardPlan":
        d = dict(d)
        d["stages"] = [StagePlan.from_dict(s) for s in d.get("stages", [])]
        return cls(**d)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_aware(dt: datetime, field_name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got naive: {dt!r}")


def _validate_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid timezone: {tz_name!r}") from exc


def _compute_plan_id(channel_id: str, target_publish_at: datetime, generated_at: datetime, tz_name: str) -> str:
    """Deterministic — no randomness. Identical inputs => identical plan_id."""
    seed = f"{channel_id}|{target_publish_at.isoformat()}|{generated_at.isoformat()}|{tz_name}"
    return f"bp_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _resolve_publishing_duration(operations_profile, overrides: Optional[dict]) -> int:
    if overrides and _PUBLISHING_STAGE in overrides:
        return overrides[_PUBLISHING_STAGE]

    enabled_platforms = operations_profile.enabled_platforms or {}
    blocked = operations_profile.blocked_platform_reasons or {}
    # Blocked/disabled platforms contribute zero duration — only genuinely
    # enabled, unblocked platforms add upload/verification time.
    active = [
        p for p, is_enabled in enabled_platforms.items()
        if is_enabled and p not in blocked and p in SUPPORTED_PLATFORMS
    ]
    return PUBLISHING_PREP_DURATION_SECONDS + sum(
        PER_PLATFORM_UPLOAD_DURATION_SECONDS.get(p, 0) for p in active
    )


def _refused_plan(
    channel_id: str,
    target_publish_at: datetime,
    generated_at: datetime,
    tz_name: str,
    reason: str,
    max_concurrent_episodes: int,
) -> BackwardPlan:
    return BackwardPlan(
        plan_id=_compute_plan_id(channel_id, target_publish_at, generated_at, tz_name),
        channel_id=channel_id,
        target_publish_at=target_publish_at.isoformat(),
        production_start_at="",
        timezone=tz_name,
        generated_at=generated_at.isoformat(),
        plan_status="refused",
        stages=[],
        max_concurrent_episodes=max_concurrent_episodes,
        refusal_reason=reason,
    )


# ---------------------------------------------------------------------------
# Pure calculation — no I/O, no API calls, no database writes.
# ---------------------------------------------------------------------------

def build_backward_plan(
    channel_id: str,
    target_publish_at: datetime,
    operations_profile,
    generated_at: datetime,
    stage_durations: Optional[dict] = None,
    safety_buffer_seconds: int = 3600,
) -> BackwardPlan:
    """
    Build a deterministic backward production plan for one episode.

    Args:
        channel_id:            Channel identifier (informational — not looked
                                up; the caller supplies operations_profile).
        target_publish_at:     Timezone-aware target publish datetime.
        operations_profile:    A hypogeum.channel_config.ChannelOperationsProfile
                                (duck-typed — only its fields are read; never
                                mutated).
        generated_at:          Timezone-aware "now" for this calculation.
                                Must be supplied by the caller (or an
                                injectable clock upstream) — this function
                                never reads the real wall clock, which is
                                what makes it pure and deterministic.
        stage_durations:       Optional per-stage overrides (seconds), merged
                                over DEFAULT_STAGE_DURATIONS_SECONDS.
        safety_buffer_seconds: Single overall margin applied once, between
                                the last stage's finish and target_publish_at
                                (the "go-live" cushion). Each individual
                                stage also carries its own smaller buffer —
                                see _PER_STAGE_SAFETY_BUFFER_FRACTION.

    Returns:
        BackwardPlan — plan_status is "executable", "infeasible", or
        "refused". Never raises for a target that's merely too close in
        time or for a disabled operator/production flag — those are
        represented in the returned plan, not exceptions. Raises ValueError
        for genuinely invalid input (naive timestamps, bad timezone, missing
        or non-positive stage duration).
    """
    _require_aware(target_publish_at, "target_publish_at")
    _require_aware(generated_at, "generated_at")

    tz_name = operations_profile.timezone
    tz = _validate_timezone(tz_name)

    max_concurrent_episodes = operations_profile.max_concurrent_episodes

    if not operations_profile.operator_enabled:
        return _refused_plan(
            channel_id, target_publish_at, generated_at, tz_name,
            reason="operator_enabled is False — operator has not authorized this channel to run",
            max_concurrent_episodes=max_concurrent_episodes,
        )
    if not operations_profile.production_enabled:
        return _refused_plan(
            channel_id, target_publish_at, generated_at, tz_name,
            reason="production_enabled is False — production is disabled for this channel",
            max_concurrent_episodes=max_concurrent_episodes,
        )
    # unattended_operation_enabled=False deliberately does NOT refuse — it
    # only means a future worker cannot run this plan unattended yet.

    merged_durations = dict(DEFAULT_STAGE_DURATIONS_SECONDS)
    if stage_durations:
        merged_durations.update(stage_durations)

    publishing_included = bool(operations_profile.publishing_enabled)
    stages_to_plan = [s for s in PIPELINE_STAGE_ORDER if s != _PUBLISHING_STAGE or publishing_included]

    if publishing_included:
        merged_durations[_PUBLISHING_STAGE] = _resolve_publishing_duration(operations_profile, stage_durations)

    for stage_name in stages_to_plan:
        duration = merged_durations.get(stage_name)
        if duration is None or duration <= 0:
            raise ValueError(
                f"missing or non-positive duration estimate for stage {stage_name!r}"
            )

    target_local = target_publish_at.astimezone(tz)
    generated_local = generated_at.astimezone(tz)

    retry_policy = operations_profile.retry_policy or {}
    max_retries = int(retry_policy.get("max_retries_per_stage", 0) or 0)
    backoff_seconds = int(retry_policy.get("backoff_seconds", 0) or 0)

    cursor = target_local - timedelta(seconds=safety_buffer_seconds)
    reversed_plans: list[StagePlan] = []

    for stage_name in reversed(stages_to_plan):
        duration = merged_durations[stage_name]
        buffer_s = round(duration * _PER_STAGE_SAFETY_BUFFER_FRACTION)
        retry_s = max_retries * (duration + backoff_seconds)
        total = duration + buffer_s + retry_s

        finish_at = cursor
        start_at = finish_at - timedelta(seconds=total)

        reversed_plans.append(StagePlan(
            stage_name=stage_name,
            sequence_index=0,  # corrected below once order is restored
            planned_start_at=start_at.isoformat(),
            planned_finish_at=finish_at.isoformat(),
            estimated_duration_seconds=duration,
            safety_buffer_seconds=buffer_s,
            retry_allowance_seconds=retry_s,
            hard_deadline_at=finish_at.isoformat(),
            depends_on=None,  # filled below
            critical=True,    # v1 has no parallel branches — every stage is on the critical path
        ))
        cursor = start_at

    stage_plans = list(reversed(reversed_plans))
    for i, sp in enumerate(stage_plans):
        sp.sequence_index = i
        sp.depends_on = stage_plans[i - 1].stage_name if i > 0 else None

    production_start_dt = cursor  # start of the very first stage
    production_start_at = production_start_dt.isoformat()

    non_publishing_names = [s for s in stages_to_plan if s != _PUBLISHING_STAGE]
    production_complete_at = None
    if non_publishing_names:
        last_production_name = non_publishing_names[-1]
        production_complete_at = next(
            sp.planned_finish_at for sp in stage_plans if sp.stage_name == last_production_name
        )

    required_lead_time_seconds = int((target_local - production_start_dt).total_seconds())
    available_lead_time_seconds = int((target_local - generated_local).total_seconds())

    plan_id = _compute_plan_id(channel_id, target_publish_at, generated_at, tz_name)

    if production_start_dt < generated_local:
        earliest_feasible = generated_local + timedelta(seconds=required_lead_time_seconds)
        return BackwardPlan(
            plan_id=plan_id,
            channel_id=channel_id,
            target_publish_at=target_local.isoformat(),
            production_start_at=production_start_at,
            timezone=tz_name,
            generated_at=generated_local.isoformat(),
            plan_status="infeasible",
            stages=stage_plans,
            max_concurrent_episodes=max_concurrent_episodes,
            production_complete_at=production_complete_at,
            required_lead_time_seconds=required_lead_time_seconds,
            available_lead_time_seconds=available_lead_time_seconds,
            earliest_feasible_publish_at=earliest_feasible.isoformat(),
            blocking_stages=[sp.stage_name for sp in stage_plans],
        )

    return BackwardPlan(
        plan_id=plan_id,
        channel_id=channel_id,
        target_publish_at=target_local.isoformat(),
        production_start_at=production_start_at,
        timezone=tz_name,
        generated_at=generated_local.isoformat(),
        plan_status="executable",
        stages=stage_plans,
        max_concurrent_episodes=max_concurrent_episodes,
        production_complete_at=production_complete_at,
        required_lead_time_seconds=required_lead_time_seconds,
        available_lead_time_seconds=available_lead_time_seconds,
    )


# ---------------------------------------------------------------------------
# Persistence — JSON artifact under the episode workspace. Separate from the
# pure calculation above; no existing table (schedules/scheduled_runs/
# run_queue) has per-stage timing fields, so a new DB table is not added.
# ---------------------------------------------------------------------------

def save_backward_plan(
    plan: BackwardPlan,
    episode_id: str,
    project_root: Optional[Path] = None,
) -> Path:
    root = project_root if project_root is not None else _PROJECT_ROOT
    episode_dir = root / ".hypogeum-assets" / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    path = episode_dir / f"backward_plan_{episode_id}.json"
    path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_backward_plan(
    episode_id: str,
    project_root: Optional[Path] = None,
) -> Optional[BackwardPlan]:
    root = project_root if project_root is not None else _PROJECT_ROOT
    path = root / ".hypogeum-assets" / "episodes" / episode_id / f"backward_plan_{episode_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return BackwardPlan.from_dict(data)
