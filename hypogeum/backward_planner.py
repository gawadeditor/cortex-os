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
  finance_agent_authorization -> thumbnail_strategy -> audio_agent ->
  visual_package_director -> thumbnail_director ->
  assembly_agent -> asset_archive -> qa_agent -> compliance_agent ->
  publishing_agent

  (2026-08-08: audio_agent moved before visual_package_director —
  narration must be measured before Cinematic Coverage Planning can plan
  shot budgets from real per-beat durations instead of a pre-audio
  word-count estimate; matches PROJECT_NORTH_STAR.md's "Operating Model"
  and WORKFLOW_MODEL.md's "Synchronization" example, both already Voice
  before Visual.)

Timing model (2026-07-21 calibration):
  Every stage's duration is decomposed into four inspectable components —
  active_execution_seconds, expected_external_wait_seconds,
  retry_allowance_seconds, safety_buffer_seconds — summed into
  total_planned_seconds (the actual span reserved in the timeline).
  estimated_duration_seconds = active_execution_seconds +
  expected_external_wait_seconds, preserved as a field for compatibility
  with the original v1 shape.

  visual_package_director and publishing_agent are workload-driven rather
  than flat constants — see DEFAULT_EXPECTED_SCENE_COUNT and
  PER_PLATFORM_UPLOAD_ALLOWANCE_SECONDS / PER_PLATFORM_VERIFICATION_ALLOWANCE_SECONDS
  below for exactly which numbers are grounded in repository evidence
  (real benchmarks, real code timeout/poll constants) versus clearly-marked
  conservative provisional assumptions (no real telemetry exists yet).

Public API:
    build_backward_plan(channel_id, target_publish_at, operations_profile,
                        generated_at, stage_durations=None,
                        safety_buffer_seconds=_DEFAULT_SAFETY_BUFFER_SECONDS (86400),
                        workload=None) -> BackwardPlan
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

_VISUAL_PACKAGE_STAGE = "visual_package_director"
_PUBLISHING_STAGE = "publishing_agent"

# ---------------------------------------------------------------------------
# Per-stage active/wait defaults — seconds. Each entry is
# (active_execution_seconds, expected_external_wait_seconds).
#
# active_execution_seconds  — work done on our side: prompt assembly, local
#                              parsing/validation, ffmpeg CPU time, etc.
# expected_external_wait_seconds — time genuinely spent waiting on a
#                              provider (LLM call, MUAPI generation/poll,
#                              ElevenLabs synthesis, platform processing).
#
# visual_package_director and publishing_agent are NOT looked up here —
# they are computed from workload (see _resolve_visual_package_duration /
# _resolve_publishing_duration) because a flat constant cannot represent
# "how many scenes" or "how many platforms," which is exactly what made the
# original v1 defaults optimistic regardless of real episode size.
#
# Evidence basis per stage (grounded vs provisional):
#   startup_checks               — provisional (local checks + a reachability ping)
#   executive_producer            — provisional (single LLM scoring call)
#   research_director             — provisional (multiple sequential Perplexity queries)
#   narrative_architect            — provisional (single LLM structuring call)
#   script_engine                 — provisional (single LLM script-generation call)
#   finance_agent_authorization    — provisional (local budget check)
#   thumbnail_strategy             — provisional (single LLM concept call)
#   thumbnail_director              — provisional (multiple image-generation calls; not
#                                     scene-driven in v1 — thumbnails are a small,
#                                     roughly-fixed count of candidates, not one per scene)
#   audio_agent                   — grounded in ELEVENLABS_TIMEOUT_SECONDS=240
#                                    (hypogeum/config.py) as the per-call ceiling,
#                                    with a provisional narration+music multi-segment
#                                    assumption on top
#   assembly_agent                 — grounded in ASSEMBLY_FFMPEG_TIMEOUT=600
#                                    (hypogeum/agents/assembly_agent.py:238), the
#                                    real hard timeout ceiling the code enforces
#   asset_archive, qa_agent, compliance_agent — provisional (local/LLM checks)
# ---------------------------------------------------------------------------

DEFAULT_STAGE_ACTIVE_WAIT_SECONDS: dict[str, tuple[int, int]] = {
    "topic_selection": (300, 0),                    # 5 min  active — candidate review/selection
    "startup_checks": (10, 20),                      # provider reachability ping
    "executive_producer": (30, 150),                 # LLM scoring call
    "research_director": (60, 840),                  # multiple sequential Perplexity queries
    "narrative_architect": (30, 270),                # LLM structuring call
    "script_engine": (60, 540),                      # LLM script-generation call
    "finance_agent_authorization": (10, 20),         # local budget check
    "thumbnail_strategy": (30, 150),                 # LLM concept call
    "thumbnail_director": (180, 720),                 # ~6 image-generation calls (3 main candidates + 3 short thumbnails)
    "audio_agent": (120, 480),                       # ElevenLabs narration+music; per-call ceiling 240s (config)
    "assembly_agent": (600, 60),                      # active=ASSEMBLY_FFMPEG_TIMEOUT (real ceiling); wait=I/O overhead
    "asset_archive": (60, 0),                         # local cataloging
    "qa_agent": (60, 240),                            # LLM-based QA checks
    "compliance_agent": (30, 150),                    # LLM-based compliance checks
}

# ---------------------------------------------------------------------------
# Visual generation workload model — the primary v1 calibration target.
#
# DEFAULT_EXPECTED_SCENE_COUNT is grounded in real completed episode
# manifests (final_render_package_ep_*.json, package_integrity.scenes_expected):
# ep_1f827369=58, ep_20260629063505=50, ep_74237788=55 (main+shorts combined,
# the workload visual_package_director actually generates in one stage).
# Average of these three real episodes ≈ 54.3, rounded to 55.
#
# GENERATION_SECONDS_PER_SCENE=240 is GROUNDED in the real production
# polling loop, not a diagnostic-script estimate: hypogeum/muapi.py's
# _poll_until_complete() — the function submit_scene() actually calls —
# polls for config.MUAPI_MAX_POLL_ATTEMPTS (default 24) x
# config.MUAPI_POLL_INTERVAL_SECONDS (default 5s) = 120s before treating the
# job as failed, and its own docstring states "Timeout is treated as a job
# failure (triggers Attempt 2 in the caller)" — i.e. the code itself
# automatically retries once at the per-scene level, below and before the
# stage-level retry_policy is ever invoked. A single scene can therefore
# legitimately take up to ~240s (2 x 120s) in completely normal operation.
# Separate isolated single-clip benchmarks corroborate the 120s figure as
# plausible for a real generation: Seedance t2v ~78s, i2v ~72s
# (scripts/seedance_diagnostic_result.json); Runway t2v ~90s, i2v ~54s
# (scripts/runway_diagnostic_result.json); Veo3 Premium 128.5s
# (hypogeum/config.py comment). Override via
# workload={"generation_seconds_per_scene": ...} as real multi-scene
# telemetry becomes available.
# ---------------------------------------------------------------------------

DEFAULT_EXPECTED_SCENE_COUNT = 55
DEFAULT_GENERATION_SECONDS_PER_SCENE = 240
_VPD_PLANNING_ACTIVE_SECONDS = 900  # fixed batched Claude scene-planning overhead (provisional)

_VALID_WORKLOAD_KEYS = frozenset({"expected_scene_count", "generation_seconds_per_scene"})
_MAX_SCENE_COUNT = 500                    # sanity bound against configuration mistakes
_MAX_GENERATION_SECONDS_PER_SCENE = 3600  # sanity bound (1 hour/scene)

# ---------------------------------------------------------------------------
# Publishing workload model — per-platform upload / processing-verification
# allowance, only for platforms that are both enabled and not blocked.
# Blocked or disabled platforms contribute exactly zero.
#
# Evidence basis:
#   tiktok verification_allowance=180  — GROUNDED: hypogeum/publishing/tiktok.py
#       _TIKTOK_POLL_MAX_ATTEMPTS=18 x _TIKTOK_POLL_INTERVAL=10s = 180s real
#       max poll window this code will actually wait.
#   instagram verification_allowance=360 — GROUNDED: hypogeum/publishing/meta.py
#       _IG_POLL_MAX_ATTEMPTS=24 x _IG_POLL_INTERVAL=15s = 360s real max poll
#       window. The prior v1 flat total (300s) was BELOW this real ceiling —
#       a genuine under-estimate this calibration corrects.
#   youtube/facebook upload_allowance and youtube processing_verification
#       (captions/thumbnail/end-screen) — PROVISIONAL: no upload-bandwidth or
#       server-side-processing telemetry exists in the repository.
# ---------------------------------------------------------------------------

PUBLISHING_PREP_DURATION_SECONDS = 300  # 5 min — metadata/description building, common to all platforms, active/local

PER_PLATFORM_UPLOAD_ALLOWANCE_SECONDS: dict[str, int] = {
    "youtube": 300,     # provisional — main + 3 shorts, chunked resumable upload
    "facebook": 180,    # provisional
    "tiktok": 120,      # provisional — chunked upload of one short
    "instagram": 120,   # provisional — staging + container creation
    "email": 60,        # provisional
}

PER_PLATFORM_VERIFICATION_ALLOWANCE_SECONDS: dict[str, int] = {
    "youtube": 300,     # provisional — captions/thumbnail/end-screen processing (no explicit poll in code)
    "facebook": 120,    # provisional
    "tiktok": 180,      # GROUNDED — real poll ceiling, see module docstring above
    "instagram": 360,   # GROUNDED — real poll ceiling, see module docstring above
    "email": 0,
}

# Retained for backward-compatible external references; publishing_agent's
# own duration is always computed from the two allowance dicts above.
PER_PLATFORM_UPLOAD_DURATION_SECONDS: dict[str, int] = {
    p: PER_PLATFORM_UPLOAD_ALLOWANCE_SECONDS[p] + PER_PLATFORM_VERIFICATION_ALLOWANCE_SECONDS[p]
    for p in PER_PLATFORM_UPLOAD_ALLOWANCE_SECONDS
}

# Backward-compatible view: the flat total per stage at DEFAULT workload
# (default scene count / generation time for visual_package_director;
# publishing_agent is excluded — its default depends entirely on which
# platforms a given channel has enabled, so there is no single default total
# to show here).
DEFAULT_STAGE_DURATIONS_SECONDS: dict[str, int] = {
    name: active + wait for name, (active, wait) in DEFAULT_STAGE_ACTIVE_WAIT_SECONDS.items()
}
DEFAULT_STAGE_DURATIONS_SECONDS[_VISUAL_PACKAGE_STAGE] = (
    _VPD_PLANNING_ACTIVE_SECONDS + DEFAULT_EXPECTED_SCENE_COUNT * DEFAULT_GENERATION_SECONDS_PER_SCENE
)

# Flat per-stage padding: a simple, deterministic, documented fraction —
# not a statistical/ML estimate.
_PER_STAGE_SAFETY_BUFFER_FRACTION = 0.10

# Overall go-live buffer default — a deliberate v1 operational policy choice,
# NOT derived from workload. 24h ensures a normal complete episode is not
# scheduled to begin on the same calendar day as its publication, leaving a
# full day of margin for human monitoring and retry headroom ahead of
# unattended go-live. See build_backward_plan()'s safety_buffer_seconds
# docstring.
_DEFAULT_SAFETY_BUFFER_SECONDS = 86400

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
    active_execution_seconds: int
    expected_external_wait_seconds: int
    estimated_duration_seconds: int   # = active_execution_seconds + expected_external_wait_seconds
    safety_buffer_seconds: int
    retry_allowance_seconds: int
    total_planned_seconds: int        # = estimated_duration_seconds + retry_allowance_seconds + safety_buffer_seconds
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


def _validate_workload(workload: Optional[dict]) -> dict:
    """Fails closed: unknown keys, non-positive values, or values beyond the
    sanity bounds all raise ValueError rather than being silently accepted
    or ignored."""
    if not workload:
        return {}

    unknown = set(workload) - _VALID_WORKLOAD_KEYS
    if unknown:
        raise ValueError(f"unknown workload override key(s): {sorted(unknown)}")

    if "expected_scene_count" in workload:
        v = workload["expected_scene_count"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"workload.expected_scene_count must be a positive int, got {v!r}")
        if v > _MAX_SCENE_COUNT:
            raise ValueError(
                f"workload.expected_scene_count {v} exceeds the sanity bound of {_MAX_SCENE_COUNT}"
            )

    if "generation_seconds_per_scene" in workload:
        v = workload["generation_seconds_per_scene"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"workload.generation_seconds_per_scene must be a positive int, got {v!r}")
        if v > _MAX_GENERATION_SECONDS_PER_SCENE:
            raise ValueError(
                f"workload.generation_seconds_per_scene {v} exceeds the sanity bound of "
                f"{_MAX_GENERATION_SECONDS_PER_SCENE}"
            )

    return dict(workload)


def _resolve_visual_package_active_wait(workload: dict) -> tuple[int, int]:
    scene_count = workload.get("expected_scene_count", DEFAULT_EXPECTED_SCENE_COUNT)
    per_scene = workload.get("generation_seconds_per_scene", DEFAULT_GENERATION_SECONDS_PER_SCENE)
    # Sequential generation — no parallelism assumed (none exists in the
    # active runtime today: no threading/asyncio in hypogeum/muapi.py or
    # hypogeum/agents/visual_package_director.py).
    return _VPD_PLANNING_ACTIVE_SECONDS, scene_count * per_scene


def _resolve_publishing_active_wait(operations_profile) -> tuple[int, int]:
    enabled_platforms = operations_profile.enabled_platforms or {}
    blocked = operations_profile.blocked_platform_reasons or {}
    # Blocked/disabled platforms contribute zero duration — only genuinely
    # enabled, unblocked platforms add upload/verification time.
    active_platforms = [
        p for p, is_enabled in enabled_platforms.items()
        if is_enabled and p not in blocked and p in SUPPORTED_PLATFORMS
    ]
    upload_total = sum(PER_PLATFORM_UPLOAD_ALLOWANCE_SECONDS.get(p, 0) for p in active_platforms)
    verification_total = sum(PER_PLATFORM_VERIFICATION_ALLOWANCE_SECONDS.get(p, 0) for p in active_platforms)
    return PUBLISHING_PREP_DURATION_SECONDS + upload_total, verification_total


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
    safety_buffer_seconds: int = _DEFAULT_SAFETY_BUFFER_SECONDS,
    workload: Optional[dict] = None,
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
        stage_durations:       Optional per-stage total-seconds overrides,
                                applied as a flat (active+wait) replacement
                                for the named stage — e.g. {"publishing_agent": 900}
                                bypasses the platform-workload computation
                                entirely for that stage.
        safety_buffer_seconds: Single overall margin applied once, between
                                the last stage's finish and target_publish_at
                                (the "go-live" cushion). Defaults to
                                _DEFAULT_SAFETY_BUFFER_SECONDS (24h) — a
                                deliberate v1 operational policy choice, not
                                workload-derived: it exists so a normal
                                complete episode is not scheduled to begin
                                on the same calendar day as publication,
                                leaving a full day for human monitoring and
                                retry headroom before unattended go-live.
                                Each individual stage also carries its own
                                smaller buffer — see
                                _PER_STAGE_SAFETY_BUFFER_FRACTION.
        workload:              Optional overrides for the workload-driven
                                visual_package_director stage:
                                {"expected_scene_count": int,
                                 "generation_seconds_per_scene": int}.
                                Unknown keys or invalid values raise
                                ValueError (fail closed) — see
                                _validate_workload.

    Returns:
        BackwardPlan — plan_status is "executable", "infeasible", or
        "refused". Never raises for a target that's merely too close in
        time or for a disabled operator/production flag — those are
        represented in the returned plan, not exceptions. Raises ValueError
        for genuinely invalid input (naive timestamps, bad timezone, missing
        or non-positive stage duration, invalid workload override).
    """
    _require_aware(target_publish_at, "target_publish_at")
    _require_aware(generated_at, "generated_at")

    tz_name = operations_profile.timezone
    tz = _validate_timezone(tz_name)
    workload = _validate_workload(workload)

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

    publishing_included = bool(operations_profile.publishing_enabled)
    stages_to_plan = [s for s in PIPELINE_STAGE_ORDER if s != _PUBLISHING_STAGE or publishing_included]

    # Resolve (active, wait) per stage — workload-driven stages computed
    # dynamically; everything else from the static defaults; stage_durations
    # (when given for a stage) wins outright as a flat total (wait=0 role
    # folded into active for simplicity of the override path).
    active_wait: dict[str, tuple[int, int]] = dict(DEFAULT_STAGE_ACTIVE_WAIT_SECONDS)
    active_wait[_VISUAL_PACKAGE_STAGE] = _resolve_visual_package_active_wait(workload)
    if publishing_included:
        active_wait[_PUBLISHING_STAGE] = _resolve_publishing_active_wait(operations_profile)

    if stage_durations:
        for stage_name, total in stage_durations.items():
            if total is None or not isinstance(total, int) or isinstance(total, bool) or total <= 0:
                raise ValueError(
                    f"missing or non-positive duration estimate for stage {stage_name!r}"
                )
            active_wait[stage_name] = (total, 0)

    for stage_name in stages_to_plan:
        pair = active_wait.get(stage_name)
        if pair is None or (pair[0] + pair[1]) <= 0:
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
        active_s, wait_s = active_wait[stage_name]
        duration = active_s + wait_s
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
            active_execution_seconds=active_s,
            expected_external_wait_seconds=wait_s,
            estimated_duration_seconds=duration,
            safety_buffer_seconds=buffer_s,
            retry_allowance_seconds=retry_s,
            total_planned_seconds=total,
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
