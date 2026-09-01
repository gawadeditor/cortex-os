"""
hypogeum/pipeline.py — Pipeline orchestrator

Single run_pipeline() entry point that executes all 10 agents in the frozen order.
This is a scaffold — every agent stub raises NotImplementedError until implemented
in M1–M5. The orchestration flow, lifecycle logging, and fail-fast checks are complete.

Agent order (frozen — architecture-index.md; Thumbnail Strategy inserted
2026-07-19, completing the architecture its own module docstring already
specified — see hypogeum/agents/thumbnail_strategy.py and
docs/architecture/THUMBNAIL_CONCEPT_MODE_CONTEXT_AUDIT.md):
  EP → RD → NA → SE → Thumbnail Strategy → VPD → TD → Audio → Assembly → Publishing

Integration contracts (must not be violated by callers or this module):
  - All environment access goes through the Config object — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - All Claude calls go through hypogeum.agent.run_agent() — never call_claude() directly
  - All schema validation goes through hypogeum.schemas.validate() — never ad-hoc checks
  - Startup verification goes through hypogeum.startup.run_startup_checks() — no bypass

Execution sequence:
  1.  Log pipeline_run_started
  2.  run_startup_checks() — StartupError raises if no API path is viable
  3.  Log pipeline_startup_verified
  4.  Executive Producer — produces validation_scorecard + topic_brief
        REJECT decision → PipelineRejectedError (pipeline halts immediately)
  5.  Research Director — produces research_package (+ optional PDR)
        PDR issued → PackageDeficiencyError (pipeline halts; operator resolves + re-runs)
  6.  Narrative Architect → narrative_package
  7.  Script Engine → script_package
  8.  Thumbnail Strategy → thumbnail_concept_brief (activates CONCEPT_DRIVEN_MODE
        downstream in Thumbnail Director)
  9.  Visual Package Director → visual_package
  10. Thumbnail Director → thumbnail_package
  11. Audio Agent → narration_package
  12. Assembly Agent → final_render_package
  13. Publishing Agent → publishing_record
  14. Log pipeline_run_completed; return PipelineResult

Logging events emitted:
  pipeline_run_started         — info  — before startup checks
  pipeline_startup_verified    — info  — after startup checks pass
  pipeline_stage_started       — info  — before each agent call
  pipeline_stage_completed     — info  — after each agent returns
  pipeline_topic_rejected      — warn  — EP scored topic REJECT
  pipeline_pdr_issued          — error — RD issued a Package Deficiency Report
  pipeline_run_completed       — info  — all 10 agents completed
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from hypogeum.agents.asset_archive_agent import run_asset_archive
from hypogeum.agents.assembly_agent import run_assembly_agent
from hypogeum.agents.audio_agent import run_audio_agent
from hypogeum.agents.cinematic_director import validate_narration_completeness
from hypogeum.agents.executive_producer import run_executive_producer
from hypogeum.agents.archive_agent import run_archive_agent
from hypogeum.agents.compliance_agent import run_compliance_agent
from hypogeum.agents.finance_agent import run_budget_authorization, run_cost_ledger
from hypogeum.agents.qa_agent import run_qa_agent
from hypogeum.agents.narrative_architect import run_narrative_architect
from hypogeum.agents.publishing_agent import run_publishing_agent
from hypogeum.agents.research_director import run_research_director
from hypogeum.agents.script_engine import run_script_engine
from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.agents.thumbnail_strategy import run_thumbnail_strategy
from hypogeum.agents.visual_package_director import (
    run_visual_package_director,
    run_visual_package_director_from_plan,
)
from hypogeum.config import Config
from hypogeum.db import (
    get_connection,
    insert_stage_execution,
    update_stage_execution_complete,
    update_stage_execution_failed,
    update_stage_execution_skipped,
)
from hypogeum.failure_policy import FailurePolicy
from hypogeum.finance import clear_run_context, set_run_context, write_episode_rollup
from hypogeum.ois import OISBlockedError, get_blocking_signals, is_channel_blocked, is_factory_blocked
from hypogeum.production_gate import require_full_production_authorized
from hypogeum.post_run_intelligence import run_post_run_intelligence
from hypogeum.production_metadata import write_analytics_pending, write_production_metadata
from hypogeum.logger import get_logger
from hypogeum.run_registry import cancel_run, complete_run, fail_run, register_run, transition_run_state
from hypogeum.startup import StartupReport, run_startup_checks
from hypogeum.workflow_executor import (
    StageOrderVerifier,
    WorkflowArtifactUnreadableError,
    WorkflowDriftError,
    resolve_workflow_binding,
    verify_and_load_workflow,
)

# The registered Workflow this module's own stage sequence must match --
# see channel_packages/hypogeum/workflow/hypogeum_main_production.json
# (the authoritative source) and scripts/register_hypogeum_workflow.py
# (which indexes it). Bumping this string is only valid alongside a
# corresponding new registration of that file's content.
_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Model slug constants (frozen — T0.1 model assignments)
# ---------------------------------------------------------------------------

_SONNET = "anthropic/claude-sonnet-4.6"
_OPUS   = "anthropic/claude-opus-4.6"


# ---------------------------------------------------------------------------
# Per-agent max_tokens (sized to expected output package)
# ---------------------------------------------------------------------------

_EP_MAX_TOKENS    = 4_096   # two compact packages (scorecard + brief)
_RD_MAX_TOKENS    = 16_384  # large research package with many claims
_NA_MAX_TOKENS    = 16_384  # full narrative architecture with reveal map
_SE_MAX_TOKENS    = 16_384  # full script with three shorts + critic pass
_VPD_MAX_TOKENS   = 4_096   # scene descriptions + entity continuity
_TD_MAX_TOKENS    = 2_048   # thumbnail candidates + selection
_AUDIO_MAX_TOKENS = 2_048   # segment manifest + stitch plan
_ASM_MAX_TOKENS   = 4_096   # timeline + render manifest
_PUB_MAX_TOKENS   = 2_048   # publishing record

_ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets"


# ---------------------------------------------------------------------------
# Stage boundary metadata (Defect B, 2026-08-17)
#
# Declares, per stage, whether it performs a paid provider operation and
# whether owner authorization is required before entering it. This is
# metadata about *this channel's* stage sequence (pipeline.py already
# hardcodes the full EP/RD/NA/SE/... stage list itself, so this table lives
# alongside it, not in any shared/generic module) -- never episode- or
# run-specific, and never referencing a specific provider name (ElevenLabs,
# MUAPI, etc.) so it stays meaningful if the underlying provider changes.
#
# requires_generation_envelope marks stages whose real, future gate is a
# Generation Execution Envelope + Authorization Authority grant (not built
# yet -- see docs/architecture plan, Factory Commit F0 onward). Until that
# authority exists, _stage_requires_unavailable_authorization() below always
# treats a requires_authorization=True stage as lacking valid authorization
# -- an honest "not yet possible" answer, never a fabricated pass.
# ---------------------------------------------------------------------------

_STAGE_BOUNDARY_METADATA: dict[str, dict] = {
    "executive_producer":          {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "research_director":           {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "narrative_architect":         {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "script_engine":                {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "finance_agent_authorization": {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "thumbnail_strategy":          {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "audio_agent":                  {"paid": True,  "requires_authorization": True,  "requires_generation_envelope": True},
    "visual_package_director":     {"paid": True,  "requires_authorization": True,  "requires_generation_envelope": True},
    "source_enhancement":          {"paid": True,  "requires_authorization": True,  "requires_generation_envelope": True},
    "thumbnail_director":          {"paid": True,  "requires_authorization": True,  "requires_generation_envelope": True},
    "assembly_agent":               {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "qa_agent":                     {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "compliance_agent":             {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
    "publishing_agent":             {"paid": False, "requires_authorization": False, "requires_generation_envelope": False},
}


def _stage_requires_unavailable_authorization(next_stage: str) -> Optional[dict]:
    """
    Returns pause evidence ({"next_stage", "required_authorization_identity"})
    if `next_stage` requires authorization that does not currently exist, or
    None if it may be entered freely (not paid/authorization-required, or a
    real authorization already covers it).

    No real Authorization Authority exists yet (Factory Commit F0 onward,
    not built by this fix), so today every requires_authorization=True stage
    always returns pause evidence -- this function's only job right now is
    to answer "would this stage need authorization" honestly and generically;
    it never fabricates an authorization that doesn't exist.
    """
    meta = _STAGE_BOUNDARY_METADATA.get(next_stage)
    if meta is None or not meta.get("requires_authorization"):
        return None
    return {
        "next_stage": next_stage,
        "required_authorization_identity": next_stage,
    }


def _raise_if_authorization_required(
    episode_id: str, stage_after: str, next_stage: str, enforce_authorization_boundaries: bool,
) -> None:
    """
    Called at every stage boundary, right alongside the existing cancel_check
    check. A no-op unless the caller explicitly opted in via
    enforce_authorization_boundaries=True (every existing production caller
    leaves this False, so behavior is unchanged for them) -- see
    PipelineAuthorizationRequiredError's docstring for why this must be a
    structurally distinct signal from cancel_check/PipelineCancelledError.
    """
    if not enforce_authorization_boundaries:
        return
    evidence = _stage_requires_unavailable_authorization(next_stage)
    if evidence is None:
        return
    raise PipelineAuthorizationRequiredError(
        episode_id=episode_id, stage_after=stage_after,
        next_stage=evidence["next_stage"],
        required_authorization_identity=evidence["required_authorization_identity"],
    )


# ---------------------------------------------------------------------------
# Checkpoint loader
# ---------------------------------------------------------------------------

def _load_package(
    episode_id: str,
    schema_name: str,
    output_dir: Optional[Path],
) -> Optional[dict]:
    """
    Load an existing agent output package from disk if it exists.

    Returns the loaded dict when the file exists and is valid JSON, or None
    when the file is absent or unreadable. Used to skip already-completed
    planning stages on resume runs (e.g., after MUAPIQuotaExhausted).

    The file path convention mirrors run_agent():
        {output_dir}/{schema_name}_{episode_id}.json
    """
    base = output_dir if output_dir is not None else (
        _ASSETS_ROOT / "episodes" / episode_id
    )
    path = base / f"{schema_name}_{episode_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _resolve_checkpoint_assets_root(output_dir: Optional[Path], episode_id: str) -> Path:
    """
    Determine the .hypogeum-assets root from output_dir or default.

    Mirrors hypogeum.agents.publishing_agent._resolve_assets_root() exactly,
    so a checkpoint check and a real publish attempt always agree on where
    a given asset_id resolves on disk.
    """
    if output_dir is not None:
        search = Path(output_dir)
        for _ in range(8):
            probe = search / ".hypogeum-assets"
            if probe.is_dir():
                return probe
            search = search.parent
        return Path(output_dir)
    return _ASSETS_ROOT


def _resolve_checkpoint_asset_path(
    asset_id: str, assets_root: Path, ext: str,
) -> Optional[Path]:
    """Resolve a relative asset_id to an absolute path. None if empty."""
    if not asset_id:
        return None
    return assets_root / f"{asset_id}{ext}"


@dataclass(frozen=True)
class _CheckpointSpec:
    """
    Declarative description of what makes a stage's disk artifact a valid,
    reusable checkpoint. New stages participate by adding a spec constant,
    not by writing a new validation function — see _load_verified_checkpoint().

    schema_name:  passed straight through to _load_package().
    is_complete:  optional predicate over the loaded package; None means
                  presence-only (identical to the plain _load_package()
                  behavior already used by EP/RD/NA/SE/Thumbnail Strategy/TD).
    asset_refs:   optional extractor returning (asset_id, extension) pairs
                  whose files must exist and be non-empty on disk; None
                  means no file check (text-only packages have nothing to
                  verify beyond their own JSON).
    compat_check: optional predicate over the loaded package deciding
                  artifact/policy compatibility (2026-08-01 artifact-
                  compatibility policy — see hypogeum/artifact_compatibility.py).
                  None means no compatibility check (a stage that hasn't
                  opted in yet). This is deliberately NOT age/timestamp
                  based — it must inspect the package's own recorded
                  provenance (e.g. an agent version field already written
                  into the package) and return False when that provenance
                  is no longer compatible with current policy, so a stale
                  checkpoint is caught even though its file is recent.
    """
    schema_name: str
    is_complete: Optional[Callable[[dict], bool]] = None
    asset_refs: Optional[Callable[[dict], list]] = None
    compat_check: Optional[Callable[[dict], bool]] = None


def _load_verified_checkpoint(
    episode_id: str,
    output_dir: Optional[Path],
    spec: "_CheckpointSpec",
    log,
) -> Optional[dict]:
    """
    Shared checkpoint loader: parse -> completion check -> asset check ->
    compatibility check.

    Returns the loaded package only if all four pass; otherwise None,
    which callers treat exactly like "no checkpoint" (safe fallback to
    regenerating the stage). Each rejection reason is logged so a
    checkpoint miss is diagnosable, not silent.

    The compatibility check (2026-08-01 artifact-compatibility policy) is
    intentionally last and separate from the asset-existence check above:
    a checkpoint can have every file present and still be
    policy-incompatible (e.g. assembled by an older assembly_agent version
    under different, now-forbidden rules) — that is a different failure
    mode than a missing file, and revalidating it here means a resumed run
    regenerates exactly the incompatible stage, not the whole episode.
    """
    pkg = _load_package(episode_id, spec.schema_name, output_dir)
    if pkg is None:
        return None

    if spec.is_complete is not None and not spec.is_complete(pkg):
        log.info(
            "pipeline_checkpoint_incomplete",
            schema=spec.schema_name,
            reason="completion_flag_false",
        )
        return None

    if spec.asset_refs is not None:
        assets_root = _resolve_checkpoint_assets_root(output_dir, episode_id)
        for asset_id, ext in spec.asset_refs(pkg):
            p = _resolve_checkpoint_asset_path(asset_id, assets_root, ext)
            if p is None or not p.exists() or p.stat().st_size == 0:
                log.info(
                    "pipeline_checkpoint_incomplete",
                    schema=spec.schema_name,
                    reason="asset_missing",
                    asset_id=asset_id,
                )
                return None

    if spec.compat_check is not None and not spec.compat_check(pkg):
        log.info(
            "pipeline_checkpoint_incompatible",
            schema=spec.schema_name,
            reason="policy_version_mismatch",
        )
        return None

    return pkg


_NARRATION_CHECKPOINT = _CheckpointSpec(
    schema_name="narration_package",
    is_complete=lambda pkg: bool(
        pkg.get("package_integrity", {}).get("production_ready")
    ),
    asset_refs=lambda pkg: [
        (t.get("audio_asset_id", ""), ".mp3")
        for t in pkg.get("stitched_tracks", [])
        if t.get("audio_asset_id")
    ],
)

def _assembly_checkpoint_compatible(pkg: dict) -> bool:
    """
    Artifact-compatibility policy (2026-08-01): a final_render_package is
    only a valid checkpoint if it was produced by the CURRENT
    assembly_agent policy version. An older version assembled the video
    under different (possibly now-forbidden — tpad/loop/reverse/uncapped
    setpts) rules; no amount of file presence makes that acceptable to
    resume from. Rejecting it here regenerates only the Assembly stage —
    the source video clips it referenced are a separate artifact type,
    evaluated independently by hypogeum.artifact_compatibility.
    evaluate_video_clip_compatibility() inside the coverage-check gate, and
    are not invalidated by this check.
    """
    from hypogeum.agents.assembly_agent import _AGENT_VERSION  # noqa: PLC0415
    from hypogeum.artifact_compatibility import evaluate_render_compatibility  # noqa: PLC0415

    return evaluate_render_compatibility(pkg, _AGENT_VERSION).reusable


_ASSEMBLY_CHECKPOINT = _CheckpointSpec(
    schema_name="final_render_package",
    # production_ready (not main_episode_complete) — the same field name
    # and semantics narration_package uses, and the broader whole-stage
    # gate (main_episode_complete is a narrower, per-deliverable signal
    # publishing_agent.py reads for its own, different purpose).
    is_complete=lambda pkg: bool(
        pkg.get("package_integrity", {}).get("production_ready")
    ),
    compat_check=_assembly_checkpoint_compatible,
    asset_refs=lambda pkg: [
        (pkg.get("episode_render", {}).get("video_asset_id", ""), ".mp4"),
        *[
            (s.get("video_asset_id", ""), ".mp4")
            for s in pkg.get("shorts_renders", [])
            if s.get("video_asset_id")
        ],
    ],
)

def _thumbnail_checkpoint_compatible(pkg: dict) -> bool:
    """
    Artifact-compatibility policy (2026-08-02): a thumbnail_package is only
    a valid checkpoint if it was produced by the CURRENT thumbnail_director
    policy version. Before this check existed, _run_thumbnail_director()
    reused ANY existing thumbnail_package.json unconditionally (presence
    only -- no completion flag, no asset check, no compatibility check at
    all) -- confirmed in production: ep_f3619022's published thumbnail
    measured mean=20.5/stddev=9.3, an exact match for the MUAPI dry-run
    placeholder signature, with a thumbnail_qa_report on disk that predates
    Check J (c_thumbnail_placeholder_detection) and was never re-validated
    on resume. Rejecting an old-version package here forces
    thumbnail_director to regenerate; deep pixel/placeholder validation of
    the actual output is the Production Output Gate's job at publish time,
    not this checkpoint's.
    """
    from hypogeum.agents.thumbnail_director import _AGENT_VERSION  # noqa: PLC0415

    produced_version = pkg.get("generation_metadata", {}).get("thumbnail_director_version")
    return produced_version == _AGENT_VERSION


_THUMBNAIL_CHECKPOINT = _CheckpointSpec(
    schema_name="thumbnail_package",
    is_complete=lambda pkg: bool(
        pkg.get("package_integrity", {}).get("production_ready")
    ),
    compat_check=_thumbnail_checkpoint_compatible,
    asset_refs=lambda pkg: [
        (pkg.get("main_thumbnail", {}).get("asset_id", ""), ".jpg"),
        *[
            (s.get("asset_id", ""), ".jpg")
            for s in pkg.get("short_thumbnails", [])
            if s.get("asset_id")
        ],
    ],
)

_PUBLISHING_CHECKPOINT = _CheckpointSpec(
    schema_name="publishing_record",
    is_complete=lambda pkg: pkg.get("package_metadata", {}).get(
        "overall_status"
    ) == "published",
    # No asset_refs: the deliverable-level idempotency guard inside
    # run_publishing_agent() (publishing_outcomes-backed) already
    # re-verifies platform state on every call. This spec only needs to
    # decide whether to re-enter the stage at all.
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """Base class for pipeline-level errors."""


class PipelineRejectedError(PipelineError):
    """
    The Executive Producer scored the topic below the REJECT threshold (overall_score < 6.0).

    No further agents are invoked when this is raised. The operator should
    review the validation_scorecard, revise the topic input, and re-run.

    Attributes:
        episode_id  — episode identifier from the failed run.
        scorecard   — the validation_scorecard dict produced by the EP.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, scorecard: dict) -> None:
        self.episode_id = episode_id
        self.scorecard = scorecard
        score = scorecard.get("overall_score", "?")
        super().__init__(
            f"Episode {episode_id!r} rejected by Executive Producer "
            f"(overall_score={score}, decision=REJECT). "
            f"Revise the topic input and re-run."
        )


class ComplianceRejectedError(PipelineError):
    """
    The Rights & Compliance Agent issued a REJECTED verdict, blocking the Publishing Agent.

    The operator must review the compliance_report, resolve all failures,
    and re-run from the compliance stage.

    Attributes:
        episode_id  — episode identifier from the failed run.
        report      — the compliance_report dict produced by the Compliance Agent.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, report: dict) -> None:
        self.episode_id = episode_id
        self.report = report
        failures = report.get("failures", [])
        super().__init__(
            f"Episode {episode_id!r} blocked by Rights & Compliance Agent "
            f"(verdict=REJECTED). Failures: {failures}. "
            f"Review compliance_report and resolve before re-running."
        )


class QARejectedError(PipelineError):
    """
    The QA Agent issued a REJECTED verdict, blocking the Publishing Agent.

    The operator must review the qa_approval_report, resolve all failures,
    and re-run from the QA stage.

    Attributes:
        episode_id  — episode identifier from the failed run.
        report      — the qa_approval_report dict produced by the QA Agent.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, report: dict) -> None:
        self.episode_id = episode_id
        self.report = report
        failures = report.get("failures", [])
        super().__init__(
            f"Episode {episode_id!r} blocked by QA Agent (verdict=REJECTED). "
            f"Failures: {failures}. "
            f"Review qa_approval_report and resolve before re-running."
        )


class FinanceBudgetBlockedError(PipelineError):
    """
    The Finance Agent issued a BLOCKED verdict, stopping generation before
    MUAPI / ElevenLabs / OpenRouter spend is incurred.

    The operator must review the finance_authorization record, raise the
    FINANCE_BUDGET_CEILING_USD, or reduce the episode scope.

    Attributes:
        episode_id  — episode identifier from the failed run.
        record      — the finance_record dict produced by the Finance Agent.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, record: dict) -> None:
        self.episode_id = episode_id
        self.record = record
        estimated = record.get("estimated_spend_usd", "?")
        ceiling = record.get("budget_ceiling_usd", "?")
        super().__init__(
            f"Episode {episode_id!r} blocked by Finance Agent (verdict=BLOCKED). "
            f"Estimated spend ${estimated} exceeds ceiling ${ceiling}. "
            f"Raise FINANCE_BUDGET_CEILING_USD or reduce episode scope."
        )


class PipelineCancelledError(PipelineError):
    """
    Operator requested cancellation between pipeline stages.

    Raised after completing the current atomic stage operation — the stage
    that was running is allowed to finish before the pipeline stops.
    Checkpoints on disk are preserved so the run can be resumed.

    Attributes:
        episode_id  — episode identifier from the cancelled run.
        stage_after — name of the stage that just completed when cancel was detected.
    """
    def __init__(self, episode_id: str, stage_after: str) -> None:
        self.episode_id = episode_id
        self.stage_after = stage_after
        super().__init__(
            f"Episode {episode_id!r} cancelled by operator after stage {stage_after!r}. "
            f"Checkpoints are preserved — resume with a rerun."
        )


class PipelineAuthorizationRequiredError(PipelineError):
    """
    The next stage requires paid-provider/owner authorization that does not
    currently exist, so the run stops here rather than entering it.

    Structurally distinct from PipelineCancelledError (operator cancellation)
    -- the two must never share one exception type or be inferred from a
    cancel_check() bool alone (see run_pipeline()'s enforce_authorization_boundaries
    parameter and _stage_requires_unavailable_authorization()). Only this
    exception may result in a 'paused' run state with a
    paused_before_paid_boundary reason; PipelineCancelledError always means
    operator-requested cancellation and always results in 'cancelled'.

    Raised only after the current stage has fully completed (its output is
    checkpointed) and only when the caller opted in via
    enforce_authorization_boundaries=True -- by default (every existing
    production caller, e.g. hypogeum.queue.process_next()) this is never
    raised and behavior is unchanged.

    Attributes:
        episode_id                    — episode identifier.
        stage_after                   — name of the stage that just completed.
        next_stage                    — name of the stage that would run next.
        required_authorization_identity — opaque identifier for what
            authorization is missing (stage name today; a real
            Generation Execution Envelope / grant identity once the
            Authorization Authority exists).
    """
    def __init__(
        self, episode_id: str, stage_after: str, next_stage: str,
        required_authorization_identity: str,
    ) -> None:
        self.episode_id = episode_id
        self.stage_after = stage_after
        self.next_stage = next_stage
        self.required_authorization_identity = required_authorization_identity
        super().__init__(
            f"Episode {episode_id!r}: stage {next_stage!r} requires authorization "
            f"({required_authorization_identity!r}) that does not currently exist. "
            f"Paused after {stage_after!r}; checkpoints are preserved — resume once "
            f"authorization is granted."
        )


class ChannelNotFoundError(PipelineError):
    """
    No channel record exists in the DB for the requested channel_id.

    Run scripts/bootstrap_channel.py to create the channel before
    invoking the pipeline with --channel-id.

    Attributes:
        channel_id — the channel_id that was not found.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        super().__init__(
            f"Channel {channel_id!r} not found in DB. "
            f"Run scripts/bootstrap_channel.py to create it, "
            f"then re-run the pipeline."
        )


class ChannelNotActiveError(PipelineError):
    """
    The channel exists in the DB but is not in 'active' status.

    The pipeline requires an active channel. Activate the channel
    (or check why it is paused/blocked) before running.

    Attributes:
        channel_id — the channel that was found.
        status     — its current status (e.g. 'configured', 'paused').
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, channel_id: str, status: str) -> None:
        self.channel_id = channel_id
        self.status = status
        super().__init__(
            f"Channel {channel_id!r} is not active (status={status!r}). "
            f"Activate the channel before running the pipeline."
        )


class PackageDeficiencyError(PipelineError):
    """
    Research Director issued a Package Deficiency Report, halting the pipeline.

    The pipeline cannot proceed until the deficiencies are resolved. The operator
    must review the PDR, address the identified issues (typically in the Topic Brief),
    and re-run the pipeline from the start.

    Attributes:
        episode_id — episode identifier from the failed run.
        pdr        — the package_deficiency_report dict issued by the RD.
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, pdr: dict) -> None:
        self.episode_id = episode_id
        self.pdr = pdr
        severity = pdr.get("severity", "?")
        super().__init__(
            f"Episode {episode_id!r}: Research Director issued a Package Deficiency Report "
            f"(severity={severity}). Pipeline halted. "
            f"Resolve the deficiencies and re-run."
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """
    Sealed result of a successfully completed pipeline run.

    All package fields are populated when status == "completed". Each field
    corresponds to the validated output of one agent — the same dict that
    was written to disk by run_agent().

    The result is not produced when the pipeline is halted by StartupError,
    PipelineRejectedError, PackageDeficiencyError, or NotImplementedError —
    those all raise before a PipelineResult is created.
    """
    episode_id: str
    status: str  # "completed"
    startup_report: StartupReport
    run_id: Optional[str] = None

    validation_scorecard: Optional[dict] = None
    topic_brief: Optional[dict] = None
    research_package: Optional[dict] = None
    narrative_package: Optional[dict] = None
    script_package: Optional[dict] = None
    finance_authorization: Optional[dict] = None
    visual_package: Optional[dict] = None
    thumbnail_package: Optional[dict] = None
    narration_package: Optional[dict] = None
    final_render_package: Optional[dict] = None
    qa_approval_report: Optional[dict] = None
    compliance_report: Optional[dict] = None
    publishing_record: Optional[dict] = None
    archive_record: Optional[dict] = None
    finance_record: Optional[dict] = None
    asset_archive_record: Optional[dict] = None
    relocation_record: Optional[dict] = None
    production_metadata_record: Optional[dict] = None
    analytics_pending_record: Optional[dict] = None
    approval_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _bind_execution_db(fn):
    """Decorator: when run_pipeline is called with an explicit db_path,
    PIN it for the whole run via hypogeum.db.execution_db so the deep
    helpers that resolve get_connection(None) -- provider PRE_PLAN
    reservations, finance record_cost, OIS signals from run-state
    transitions -- write to THIS execution's database, not the process
    default. See the 2026-08-28 isolation incident (a probe passing
    db_path=<temp> still wrote run / lease / reservation / signal rows to
    production). db_path=None is byte-identical passthrough: every call
    falls through to hypogeum.db._DEFAULT_DB_PATH (the live worker relies
    on that; tests patch the constant). The decorator keeps run_pipeline
    a single FunctionDef so its real stage-order call sites stay visible
    to tooling."""
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        db_path = kwargs.get("db_path")
        if db_path is None and len(args) >= 7:
            db_path = args[6]
        if db_path is None:
            return fn(*args, **kwargs)
        import hypogeum.db as _db

        with _db.execution_db(db_path):
            return fn(*args, **kwargs)

    return _wrapped


@_bind_execution_db
def run_pipeline(
    topic_input: str,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
    youtube_privacy: str = "public",
    channel_id: Optional[str] = "hypogeum",
    db_path: Optional[Path] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    publishing_platform_filter: Optional[frozenset] = None,
    publishing_provider_by_platform: Optional[dict] = None,
    enforce_authorization_boundaries: bool = False,
) -> PipelineResult:
    """
    Execute the full 9-agent pipeline end-to-end.

    Args:
        topic_input:      The topic idea string passed to the Executive Producer.
        enforce_authorization_boundaries: When True, the run stops
                          (PipelineAuthorizationRequiredError, resulting in a
                          'paused' run state) immediately before any stage
                          marked requires_authorization in
                          _STAGE_BOUNDARY_METADATA, if no valid stage-level
                          authorization artifact exists for it. This is the
                          older explicit-artifact boundary and is separate
                          from the Authorization Authority / RunAuthorizationGrant
                          enforcement that now gates every paid provider call
                          at the provider boundary (hypogeum.muapi /
                          openrouter / ... -- see hypogeum.authorization_authority
                          and hypogeum.runtime_admission). Defaults to False,
                          matching every existing production caller
                          (hypogeum.queue.process_next() via
                          worker_integration.py) unchanged -- real autonomous
                          production runs are gated at the provider boundary,
                          not by this flag.
                          Structurally independent of cancel_check, which
                          always means operator-requested cancellation (see
                          PipelineCancelledError / PipelineAuthorizationRequiredError
                          docstrings).
                          e.g. "The Antikythera Mechanism"
        episode_id:       Unique identifier for this episode run. Used for log
                          correlation and asset file naming (see run_agent docs).
        config:           Loaded Config instance. Never access os.getenv() directly.
        output_dir:       Override directory for all agent output files.
                          Defaults to .hypogeum-assets/episodes/{episode_id}/.
                          Override in tests to redirect to a temp directory.
        youtube_privacy:  YouTube privacy for all uploads — "public", "private", or
                          "unlisted". Default "public". Pass "private" for Episode Zero
                          and test runs to prevent accidental public publishing.
        channel_id:       Channel identifier. Default "hypogeum". Model assignments
                          and voice ID are loaded from the DB channel profile and
                          applied on top of .env defaults.
                          Raises ChannelNotFoundError if the channel does not exist.
                          Raises ChannelNotActiveError if status != 'active'.
                          Pass None explicitly to bypass channel loading and use
                          .env configuration only.
        db_path:          Path to the SQLite audit database. Defaults to
                          .hypogeum-assets/hypogeum.db. Pass a tmp_path in
                          tests to isolate stage_executions writes.
        publishing_platform_filter: Optional frozenset of platform names
                          (e.g. {"youtube"}). When given, only these
                          platforms are attempted by the Publishing Agent —
                          disabled/blocked platforms never reach a provider
                          call. None means no filter (every platform with
                          valid credentials is attempted — unchanged
                          default behavior for callers that don't pass
                          this). An empty frozenset blocks every platform
                          (used when publishing_enabled is False upstream).
                          See hypogeum.agents.publishing_agent.run_publishing_agent's
                          enabled_platforms parameter.
        publishing_provider_by_platform: Optional {platform: provider_id}
                          mapping (ADR-013 Decision 7, item 1) — forwarded
                          unchanged to run_publishing_agent()'s parameter of
                          the same name. None (default, every existing
                          caller) preserves prior behavior exactly.

    Returns:
        PipelineResult — all 11 packages populated on success.

    Raises:
        ChannelNotFoundError   — channel_id provided but not in DB.
        ChannelNotActiveError  — channel exists but status != 'active'.
        StartupError           — no viable API path (OpenRouter unavailable AND
                                 no ANTHROPIC_API_KEY). Pipeline never starts.
        PipelineRejectedError  — EP scored topic below REJECT threshold (< 6.0).
        PackageDeficiencyError — RD issued a PDR. Operator must resolve and re-run.
        NotImplementedError    — agent stub not yet implemented (M1–M5 deliverables).
        AgentError             — run_agent() failed (parse, schema, or write error).
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")

    # -----------------------------------------------------------------------
    # -1. Channel config resolution — before any agent, spend, or registration
    # -----------------------------------------------------------------------
    if channel_id is not None:
        from hypogeum.channel_config import load_channel_config
        channel_config = load_channel_config(channel_id)
        if channel_config is None:
            raise ChannelNotFoundError(channel_id)
        if channel_config.channel.status != "active":
            raise ChannelNotActiveError(channel_id, channel_config.channel.status)
        config.apply_channel_config(channel_config)
        # ADR-013 Decision 7, item 3: thread the Channel Package's persisted
        # publishing-provider preference into the existing runtime
        # parameter. Only applied when the caller did not already pass an
        # explicit override (mirrors Config.apply_channel_config()'s own
        # "only non-empty fields replace defaults" convention) -- an
        # explicit caller-supplied publishing_provider_by_platform always
        # wins. Empty dict on the profile (the default) means no override
        # for any platform, so this is a no-op for every channel that
        # hasn't set a preference.
        if (
            publishing_provider_by_platform is None
            and channel_config.publishing is not None
            and channel_config.publishing.publishing_provider_by_platform
        ):
            publishing_provider_by_platform = dict(
                channel_config.publishing.publishing_provider_by_platform
            )

        # Per-channel runtime overrides from the channel's operations profile
        # (multi-channel factory: each channel carries its own cadence and
        # per-episode cost ceiling; the ChannelConfig bundle above only
        # carries brand/publishing/model fields).
        try:
            from hypogeum.channel_config import get_channel_operations_profile
            _ops = get_channel_operations_profile(channel_id, db_path=db_path)
        except Exception:
            _ops = None

        # (a) Short-native cadence: a shorts-only channel (videos_per_week == 0
        # and shorts_per_week >= 1) produces one vertical ~45-60s deliverable
        # per run. An explicit SHORT_NATIVE_MODE=false in the environment
        # opts out.
        if not config.SHORT_NATIVE_MODE:
            _cadence = (_ops.production_cadence if _ops is not None else {}) or {}
            _vpw = _cadence.get("videos_per_week")
            _spw = _cadence.get("shorts_per_week")
            if _vpw == 0 and isinstance(_spw, int) and _spw >= 1:
                # Pass the channel's real per-clip length so the scene cap
                # scales — a 5s-clip model needs more scenes than the default
                # 8 (sized for 8s clips) to hold a full story, or the ending
                # beats get no video and the short is cut off.
                try:
                    from hypogeum.muapi import get_requested_clip_duration_seconds  # noqa: PLC0415
                    _clip_s = get_requested_clip_duration_seconds(config, channel_id)
                except Exception:  # noqa: BLE001
                    _clip_s = None
                config.apply_short_native_preset(clip_seconds=_clip_s)
                log.info("pipeline_short_native_mode", channel_id=channel_id,
                         shorts_per_week=_spw, clip_seconds=_clip_s,
                         max_scenes=config.EPISODE_ZERO_MAX_SCENES)

        # (b) Per-channel per-episode cost ceiling. When the channel sets an
        # explicit cost_ceiling_usd_per_episode it overrides the global
        # FINANCE_BUDGET_CEILING_USD for THIS run only -- every finance gate
        # (finance_agent, preplan_reservation, production_gate) reads
        # config.FINANCE_BUDGET_CEILING_USD, so setting it here is enough.
        # A channel with no override inherits the factory/env ceiling.
        _channel_ceiling = getattr(_ops, "cost_ceiling_usd_per_episode", None) if _ops is not None else None
        if isinstance(_channel_ceiling, (int, float)) and _channel_ceiling > 0:
            if float(_channel_ceiling) != float(config.FINANCE_BUDGET_CEILING_USD or 0.0):
                log.info("pipeline_channel_cost_ceiling_applied", channel_id=channel_id,
                         channel_ceiling_usd=float(_channel_ceiling),
                         prior_env_ceiling_usd=float(config.FINANCE_BUDGET_CEILING_USD or 0.0))
            config.FINANCE_BUDGET_CEILING_USD = float(_channel_ceiling)

    # -----------------------------------------------------------------------
    # -0.5. OIS factory/channel block check — before run registration
    # If any active blocking signals exist the pipeline must not start.
    # Wrapped in broad except so OIS unavailability never breaks startup.
    # -----------------------------------------------------------------------
    try:
        if is_factory_blocked():
            raise OISBlockedError(get_blocking_signals())
        if channel_id is not None and is_channel_blocked(channel_id):
            raise OISBlockedError(get_blocking_signals(channel_id=channel_id))
    except OISBlockedError:
        raise
    except Exception:
        pass  # OIS unavailable — don't block the pipeline

    # -----------------------------------------------------------------------
    # -0.4. Expensive-run safety gate — before run registration, before any
    # agent or spend. Unlike the OIS check above, this does NOT fail open:
    # ProductionGateBlocked propagates unless full production is explicitly
    # authorized (Config.FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE!=
    # "validation", a configured max spend, and passed preflight). See
    # hypogeum/production_gate.py.
    # -----------------------------------------------------------------------
    require_full_production_authorized(config, log=log)

    # -----------------------------------------------------------------------
    # -0.3. Workflow Registry verification — before any agent, before run
    # registration. Proves this exact stage sequence matches the registered,
    # versioned Channel Package workflow artifact (channel_packages/{channel_id}/
    # workflow/*.json) before dispatching stage 1. Fails closed
    # (WorkflowArtifactUnreadableError / WorkflowDriftError) on any mismatch,
    # missing registration, or unreadable/corrupt artifact file -- never a
    # warning, never a silent continuation. Skipped only when channel_id is
    # explicitly None, mirroring the existing "bypass channel loading" contract
    # a few lines above.
    # -----------------------------------------------------------------------
    _stage_verifier: Optional[StageOrderVerifier] = None
    _workflow_binding: Optional[dict] = None
    if channel_id is not None:
        _workflow_definition = verify_and_load_workflow(channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION, registry_db_path=db_path)
        _stage_verifier = StageOrderVerifier(_workflow_definition["stage_order"])
        _workflow_binding = resolve_workflow_binding(channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION, registry_db_path=db_path)

    def _advance_stage_order(stage_name: str) -> None:
        """Real-time proof that this stage starting now is exactly the
        next one the verified, registered workflow definition expects --
        see hypogeum.workflow_executor.StageOrderVerifier. No-op when
        _stage_verifier is None (channel_id was explicitly bypassed)."""
        if _stage_verifier is not None:
            _stage_verifier.advance(stage_name)

    log.info(
        "pipeline_run_started",
        episode_id=episode_id,
        topic_input=topic_input,
        channel_id=channel_id,
    )

    # -----------------------------------------------------------------------
    # 0. Run registration — must happen before any agent or spend
    # -----------------------------------------------------------------------
    # run_id is set inside the try so the except can always reference it safely.
    run_id: Optional[str] = None
    # Tracks the in-progress stage for audit failure recording in the except block.
    _current_stage_id: Optional[str] = None
    _current_stage_t0: str = ""
    # The whole run — plan compile AND every asset submission — sees one active
    # channel, so per-channel brand identity (hypogeum.brand.channel_identity)
    # reaches every generation surface and the plan's declared prompt
    # fingerprints match what the submission gate produces.
    from hypogeum.brand import channel_identity as _channel_identity  # noqa: PLC0415
    _channel_id_token = _channel_identity._ACTIVE_CHANNEL.set(channel_id)
    try:
        run_id = register_run(episode_id=episode_id, config=config, channel_id=channel_id, workflow_binding=_workflow_binding, db_path=db_path)
        set_run_context(run_id=run_id, episode_id=episode_id, db_path=db_path)
        log.info("pipeline_run_registered", run_id=run_id, channel_id=channel_id)

        # -----------------------------------------------------------------------
        # 1. Startup verification — fail-fast before any agent is called
        # -----------------------------------------------------------------------
        _advance_stage_order("startup_checks")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "startup_checks", log,
            db_path=db_path,
        )
        startup_report = run_startup_checks(config, episode_id=episode_id)
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None

        log.info(
            "pipeline_startup_verified",
            overall_status=startup_report.overall_status,
            openrouter_reachable=startup_report.openrouter_reachable,
            fallback_active=startup_report.fallback_active,
        )

        # -----------------------------------------------------------------------
        # 2. Executive Producer
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="executive_producer")
        _advance_stage_order("executive_producer")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "executive_producer", log,
            model_slug=getattr(config, "EP_MODEL", None),
            output_schema="validation_scorecard",
            db_path=db_path,
        )
        validation_scorecard, topic_brief = _run_executive_producer(
            topic_input=topic_input,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None

        # Fail-fast: REJECT halts the pipeline before any downstream agent is called.
        if validation_scorecard.get("decision") == "REJECT":
            log.warn(
                "pipeline_topic_rejected",
                episode_id=episode_id,
                overall_score=validation_scorecard.get("overall_score"),
                decision="REJECT",
            )
            raise PipelineRejectedError(episode_id=episode_id, scorecard=validation_scorecard)

        log.info(
            "pipeline_stage_completed",
            agent="executive_producer",
            decision=validation_scorecard.get("decision"),
            overall_score=validation_scorecard.get("overall_score"),
        )
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="executive_producer")
        _raise_if_authorization_required(episode_id, "executive_producer", "research_director", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 3. Research Director
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="research_director")
        _advance_stage_order("research_director")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "research_director", log,
            model_slug=getattr(config, "RD_MODEL", None),
            output_schema="research_package",
            db_path=db_path,
        )
        research_package, pdr = _run_research_director(
            topic_brief=topic_brief,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None

        # Fail-fast: PDR halts the pipeline; operator resolves deficiencies and re-runs.
        if pdr is not None:
            log.error(
                "pipeline_pdr_issued",
                episode_id=episode_id,
                severity=pdr.get("severity"),
                issuing_agent="research_director",
            )
            raise PackageDeficiencyError(episode_id=episode_id, pdr=pdr)

        log.info("pipeline_stage_completed", agent="research_director")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="research_director")
        _raise_if_authorization_required(episode_id, "research_director", "narrative_architect", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 4. Narrative Architect
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="narrative_architect")
        _advance_stage_order("narrative_architect")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "narrative_architect", log,
            output_schema="narrative_package",
            db_path=db_path,
        )
        narrative_package = _run_narrative_architect(
            research_package=research_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="narrative_architect")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="narrative_architect")
        _raise_if_authorization_required(episode_id, "narrative_architect", "script_engine", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 5. Script Engine
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="script_engine")
        _advance_stage_order("script_engine")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "script_engine", log,
            output_schema="script_package",
            db_path=db_path,
        )
        script_package = _run_script_engine(
            narrative_package=narrative_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="script_engine")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="script_engine")
        _raise_if_authorization_required(episode_id, "script_engine", "finance_agent_authorization", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 6. Finance Agent — budget authorization gate (pre-generation spend)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="finance_agent_authorization")
        _advance_stage_order("finance_agent_authorization")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "finance_agent_authorization", log,
            output_schema="finance_authorization",
            db_path=db_path,
        )
        finance_authorization = _run_budget_authorization(
            script_package=script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="finance_agent_authorization",
            verdict=finance_authorization.get("verdict"),
            estimated_spend_usd=finance_authorization.get("estimated_spend_usd"),
        )

        # Fail-fast: BLOCKED verdict stops generation before provider spend.
        if finance_authorization.get("verdict") == "BLOCKED":
            log.error(
                "pipeline_finance_blocked",
                episode_id=episode_id,
                estimated_spend_usd=finance_authorization.get("estimated_spend_usd"),
                budget_ceiling_usd=finance_authorization.get("budget_ceiling_usd"),
            )
            raise FinanceBudgetBlockedError(episode_id=episode_id, record=finance_authorization)

        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="finance_agent_authorization")
        _raise_if_authorization_required(episode_id, "finance_agent_authorization", "thumbnail_strategy", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 6b. Thumbnail Strategy — produces thumbnail_concept_brief, which
        # activates CONCEPT_DRIVEN_MODE for Thumbnail Director below. Runs
        # between Script Engine and Visual Package Director per this
        # module's own designed architecture position (see
        # hypogeum/agents/thumbnail_strategy.py's docstring) — placed after
        # the finance gate so a BLOCKED verdict skips even this small spend.
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="thumbnail_strategy")
        _advance_stage_order("thumbnail_strategy")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "thumbnail_strategy", log,
            output_schema="thumbnail_concept_brief",
            db_path=db_path,
        )
        thumbnail_concept_brief = _run_thumbnail_strategy(
            topic_brief=topic_brief,
            script_package=script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="thumbnail_strategy",
            concepts_count=len(thumbnail_concept_brief.get("concepts", [])),
        )
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="thumbnail_strategy")
        _raise_if_authorization_required(episode_id, "thumbnail_strategy", "audio_agent", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 7. Audio Agent (Voice) — moved before Visual Package Director.
        # The canonical dependency is script_package -> narration
        # generation -> validated narration_package with measured
        # per-beat durations -> Visual Package Director -> Cinematic
        # Coverage Planning (PROJECT_NORTH_STAR.md "Operating Model",
        # WORKFLOW_MODEL.md "Synchronization" example, DATA_MODEL.md
        # "Content produced during execution", and CURRENT_STATE.md
        # "Production Pipeline" all already list Voice before Visual —
        # this reorder brings the implementation into compliance with
        # already-canonical documentation, not a new architectural
        # position). Only Audio Agent moves: it depends solely on
        # script_package (verified — see _run_audio_agent()'s signature),
        # so this is a safe, isolated reorder. Thumbnail Director still
        # depends on visual_package and stays after Visual Package Director.
        # -----------------------------------------------------------------------
        # Suppress Short narration tracks when dedicated short generation is disabled
        # (default) or when this is an Episode Zero / validation run.
        _skip_shorts = (
            episode_id == "ep_000"
            or config.SHORT_NATIVE_MODE
            or config.EPISODE_ZERO_MAX_SCENES > 0
            or not config.ENABLE_DEDICATED_SHORT_GENERATION
        )
        log.info("pipeline_stage_started", agent="audio_agent")
        _advance_stage_order("audio_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "audio_agent", log,
            output_schema="narration_package",
            db_path=db_path,
        )
        narration_package = _run_audio_agent(
            script_package=script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            skip_shorts=_skip_shorts,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="audio_agent")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="audio_agent")
        _raise_if_authorization_required(episode_id, "audio_agent", "visual_package_director", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 7a-pre. Fail-closed narration completeness gate (2026-08-08).
        # Canonical fresh production must stop here -- before Visual Package
        # Director, and therefore before run_cinematic_director() -- unless
        # every authoritative beat in script_package has exactly one valid
        # measured duration in the narration_package Audio Agent just
        # produced. See hypogeum.agents.cinematic_director.
        # validate_narration_completeness() for the full rejection list.
        # This is deliberately a separate call, not a change to VPD or
        # Cinematic Director themselves -- those keep their existing
        # word-count-estimate fallback for legacy/direct callers.
        # -----------------------------------------------------------------------
        validate_narration_completeness(script_package, narration_package)

        # -----------------------------------------------------------------------
        # 7a. Visual Package Director — now receives the validated
        # narration_package so Cinematic Coverage Planning can plan shot
        # budgets from measured per-beat narration durations instead of a
        # pre-audio word-count estimate.
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="visual_package_director")
        _advance_stage_order("visual_package_director")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "visual_package_director", log,
            output_schema="visual_package",
            db_path=db_path,
        )
        visual_package = _run_visual_package_director(
            script_package=script_package,
            narration_package=narration_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="visual_package_director")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="visual_package_director")
        _raise_if_authorization_required(episode_id, "visual_package_director", "source_enhancement", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 7b. Source Enhancement — provider-agnostic resolution-compliance
        # stage between Source Generation (above) and Coverage. See
        # hypogeum/source_enhancement.py for the full architecture.
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="source_enhancement")
        _advance_stage_order("source_enhancement")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "source_enhancement", log,
            output_schema="visual_package",
            db_path=db_path,
        )
        visual_package = _run_source_enhancement(
            visual_package=visual_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="source_enhancement")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="source_enhancement")
        _raise_if_authorization_required(episode_id, "source_enhancement", "thumbnail_director", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 7c. Thumbnail Director
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="thumbnail_director")
        _advance_stage_order("thumbnail_director")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "thumbnail_director", log,
            model_slug=getattr(config, "TD_PLANNING_MODEL", None),
            output_schema="thumbnail_package",
            db_path=db_path,
        )
        thumbnail_package = _run_thumbnail_director(
            visual_package=visual_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="thumbnail_director")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="thumbnail_director")
        _raise_if_authorization_required(episode_id, "thumbnail_director", "assembly_agent", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 8. Assembly Agent
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="assembly_agent")
        _advance_stage_order("assembly_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "assembly_agent", log,
            output_schema="final_render_package",
            db_path=db_path,
        )
        final_render_package = _run_assembly_agent(
            visual_package=visual_package,
            narration_package=narration_package,
            thumbnail_package=thumbnail_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            script_package=script_package,
            source_registry=research_package.get("source_registry", []),
            topic_brief=topic_brief,
            channel_id=channel_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="assembly_agent")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="assembly_agent")
        _raise_if_authorization_required(episode_id, "assembly_agent", "qa_agent", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 9b. Asset Archive — catalog all generated assets (non-blocking)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="asset_archive")
        _advance_stage_order("asset_archive")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "asset_archive", log,
            db_path=db_path,
        )
        asset_archive_record = _run_asset_archive(
            visual_package=visual_package,
            narration_package=narration_package,
            thumbnail_package=thumbnail_package,
            final_render_package=final_render_package,
            episode_id=episode_id,
            output_dir=output_dir,
            run_id=run_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="asset_archive",
            verdict=asset_archive_record.get("verdict"),
            total_assets_indexed=asset_archive_record.get("catalog_summary", {}).get("total_assets_indexed", 0),
        )

        # -----------------------------------------------------------------------
        # 10. QA Agent — pre-publish gate
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="qa_agent")
        _advance_stage_order("qa_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "qa_agent", log,
            output_schema="qa_approval_report",
            db_path=db_path,
        )
        qa_approval_report = _run_qa_agent(
            final_render_package=final_render_package,
            thumbnail_package=thumbnail_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="qa_agent",
            verdict=qa_approval_report.get("verdict"),
        )

        # Fail-fast: REJECTED verdict blocks Publishing Agent.
        if qa_approval_report.get("verdict") == "REJECTED":
            log.error(
                "pipeline_qa_rejected",
                episode_id=episode_id,
                failures=qa_approval_report.get("failures", []),
            )
            raise QARejectedError(episode_id=episode_id, report=qa_approval_report)

        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="qa_agent")
        _raise_if_authorization_required(episode_id, "qa_agent", "compliance_agent", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 11. Rights & Compliance Agent
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="compliance_agent")
        _advance_stage_order("compliance_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "compliance_agent", log,
            output_schema="compliance_report",
            db_path=db_path,
        )
        compliance_report = _run_compliance_agent(
            final_render_package=final_render_package,
            source_registry=research_package.get("source_registry", []),
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="compliance_agent",
            verdict=compliance_report.get("verdict"),
        )

        # Fail-fast: REJECTED verdict blocks Publishing Agent.
        if compliance_report.get("verdict") == "REJECTED":
            log.error(
                "pipeline_compliance_rejected",
                episode_id=episode_id,
                failures=compliance_report.get("failures", []),
            )
            raise ComplianceRejectedError(episode_id=episode_id, report=compliance_report)

        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="compliance_agent")
        _raise_if_authorization_required(episode_id, "compliance_agent", "publishing_agent", enforce_authorization_boundaries)

        # -----------------------------------------------------------------------
        # 11b. Approval gate — human review required by compliance report
        # When compliance_report contains "human_review_required": True, the
        # pipeline pauses here and waits for operator approval. The run is
        # transitioned to awaiting_review via RLG. The operator uses
        # scripts/approve_run.py to approve; that call transitions the run to
        # awaiting_publish_approval and resumes from publishing on the next run.
        # -----------------------------------------------------------------------
        if compliance_report.get("human_review_required"):
            from hypogeum.approvals import create_approval_request as _create_approval_req
            try:
                approval_id = _create_approval_req(
                    run_id=run_id,
                    episode_id=episode_id,
                    approval_type="human_review",
                    requested_by="system:pipeline",
                    evidence={"compliance_verdict": compliance_report.get("verdict")},
                    db_path=db_path,
                )
            except Exception as _ae:
                approval_id = None
                log.warn("pipeline_approval_request_failed", error=str(_ae)[:200])

            try:
                transition_run_state(
                    run_id=run_id,
                    to_state="awaiting_review",
                    triggered_by="system:pipeline",
                    trigger_source="pipeline",
                    reason="human_review_required by compliance gate",
                    db_path=db_path,
                    episode_id=episode_id,
                )
            except Exception as _te:
                log.warn("pipeline_rlg_transition_failed", to_state="awaiting_review",
                         error=str(_te)[:200])

            log.info(
                "pipeline_awaiting_human_review",
                approval_id=approval_id,
                run_id=run_id,
                episode_id=episode_id,
            )
            try:
                clear_run_context()
            except Exception:
                pass

            return PipelineResult(
                episode_id=episode_id,
                status="awaiting_review",
                startup_report=startup_report,
                run_id=run_id,
                approval_id=approval_id,
                validation_scorecard=validation_scorecard,
                topic_brief=topic_brief,
                research_package=research_package,
                narrative_package=narrative_package,
                script_package=script_package,
                finance_authorization=finance_authorization,
                visual_package=visual_package,
                thumbnail_package=thumbnail_package,
                narration_package=narration_package,
                final_render_package=final_render_package,
                qa_approval_report=qa_approval_report,
                compliance_report=compliance_report,
            )

        # -----------------------------------------------------------------------
        # 12. Publishing Agent
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="publishing_agent")
        _advance_stage_order("publishing_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "publishing_agent", log,
            output_schema="publishing_record",
            db_path=db_path,
        )
        # Resolve publish schedule. The autonomous worker path never passes
        # youtube_privacy, so it defaults "public". We override: if a schedule
        # exists → private + publishAt; if not → block (no public uploads).
        _episode_publish_at = _get_episode_publish_at(episode_id, db_path)
        publishing_record = _run_publishing_agent(
            final_render_package=final_render_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            youtube_privacy=youtube_privacy,
            publish_at=_episode_publish_at,
            enabled_platforms=publishing_platform_filter,
            publishing_provider_by_platform=publishing_provider_by_platform,
            channel_id=channel_id,
            db_path=db_path,
            assets_root=(Path(output_dir).parent.parent if output_dir is not None else None),
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="publishing_agent")

        # -----------------------------------------------------------------------
        # 13. Archive Agent — post-publish audit record (non-blocking)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="archive_agent")
        _advance_stage_order("archive_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "archive_agent", log,
            db_path=db_path,
        )
        archive_record = _run_archive_agent(
            final_render_package=final_render_package,
            publishing_record=publishing_record,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="archive_agent",
            verdict=archive_record.get("verdict"),
            archived=archive_record.get("archived"),
        )

        # -----------------------------------------------------------------------
        # 14. Finance Agent — cost ledger record (post-run, non-blocking)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="finance_agent_ledger")
        _advance_stage_order("finance_agent_ledger")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "finance_agent_ledger", log,
            db_path=db_path,
        )
        finance_record = _run_cost_ledger(
            final_render_package=final_render_package,
            publishing_record=publishing_record,
            archive_record=archive_record,
            config=config,
            episode_id=episode_id,
            narration_package=narration_package,
            visual_package=visual_package,
            output_dir=output_dir,
            run_id=run_id,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        if run_id is not None:
            try:
                from hypogeum.run_registry import renew_run_lease
                renew_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        _current_stage_id = None
        log.info(
            "pipeline_stage_completed",
            agent="finance_agent_ledger",
            verdict=finance_record.get("verdict"),
        )

        # -----------------------------------------------------------------------
        # 15. Production metadata — durable production snapshot
        # -----------------------------------------------------------------------
        _selected_thumb = thumbnail_package.get("main_thumbnail", {}) if thumbnail_package else {}
        _narrative_struct = {
            k: narrative_package.get(k)
            for k in ("narrative_arc", "structure_type", "total_segments")
            if k in (narrative_package or {})
        }
        _title_text = script_package.get("title", "") if script_package else ""
        from hypogeum.identifiers import generate_title_id
        _title_id = generate_title_id(_title_text, episode_id) if _title_text else ""
        _source_count = len(research_package.get("source_registry", [])) if research_package else 0
        _model_assignments = {
            "ep": getattr(config, "EP_MODEL", ""),
            "rd": getattr(config, "RD_MODEL", ""),
            "td_planning": getattr(config, "TD_PLANNING_MODEL", ""),
            "bga_pass1": getattr(config, "BGA_PASS1_MODEL", ""),
            "bga_pass2": getattr(config, "BGA_PASS2_MODEL", ""),
        }
        production_metadata_record = write_production_metadata(
            run_id=run_id,
            episode_id=episode_id,
            title_id=_title_id,
            selected_title=_title_text,
            selected_thumbnail=_selected_thumb,
            narrative_structure=_narrative_struct,
            research_source_count=_source_count,
            model_assignments=_model_assignments,
        )

        # Phase 5 — post-run intelligence (non-blocking)
        _source_registry = research_package.get("source_registry", []) if research_package else []
        run_post_run_intelligence(
            episode_id=episode_id,
            production_metadata=production_metadata_record,
            source_registry=_source_registry,
            run_id=run_id,
            analytics_snapshot=None,  # not available at this stage; signal flow skipped
        )

        # analytics_pending — only after confirmed YouTube publish
        analytics_pending_record = None
        _yt_id = publishing_record.get("summary", {}).get("youtube_main_video_id", "") if publishing_record else ""
        _yt_published = publishing_record.get("summary", {}).get("youtube_main_published", False) if publishing_record else False
        _published_at = publishing_record.get("published_at", "") if publishing_record else ""
        if _yt_published and _yt_id:
            analytics_pending_record = write_analytics_pending(
                episode_id=episode_id,
                youtube_video_id=_yt_id,
                published_at=_published_at,
                run_id=run_id,
            )

        # -----------------------------------------------------------------------
        # Archive Relocation — move the published episode's asset tree to the
        # configured archive root and repoint the catalog. Terminal, non-
        # blocking, and NOT a verified workflow stage (it changes no agent
        # output and must never gate a run): it runs after the frozen stage
        # sequence has completed. Only acts when the episode has a succeeded
        # publication — relocate_episode_assets() enforces that itself.
        # -----------------------------------------------------------------------
        relocation_record = None
        _pub_overall = (
            (publishing_record or {}).get("package_metadata", {}).get("overall_status", "")
        )
        if _pub_overall in ("published", "partially_published") and \
                (archive_record or {}).get("verdict") in ("ARCHIVED", "WARNING"):
            log.info("archive_relocation_started", episode_id=episode_id)
            try:
                relocation_record = _run_archive_relocation(
                    episode_id=episode_id,
                    channel_id=channel_id,
                    output_dir=output_dir,
                    db_path=db_path,
                )
            except Exception as _exc:  # noqa: BLE001
                log.warn("archive_relocation_errored", episode_id=episode_id, error=str(_exc))
            log.info(
                "archive_relocation_completed",
                episode_id=episode_id,
                verdict=(relocation_record or {}).get("verdict"),
                files_moved=(relocation_record or {}).get("files_moved", 0),
            )

        # -----------------------------------------------------------------------
        # 16. Seal result
        # -----------------------------------------------------------------------
        complete_run(run_id=run_id, episode_id=episode_id, db_path=db_path)
        write_episode_rollup(run_id=run_id, episode_id=episode_id, db_path=db_path)
        clear_run_context()
        try:
            from hypogeum.run_registry import release_run_lease
            release_run_lease(run_id, db_path=db_path)
        except Exception:
            pass

        result = PipelineResult(
            episode_id=episode_id,
            status="completed",
            startup_report=startup_report,
            run_id=run_id,
            validation_scorecard=validation_scorecard,
            topic_brief=topic_brief,
            research_package=research_package,
            narrative_package=narrative_package,
            script_package=script_package,
            finance_authorization=finance_authorization,
            visual_package=visual_package,
            thumbnail_package=thumbnail_package,
            narration_package=narration_package,
            final_render_package=final_render_package,
            qa_approval_report=qa_approval_report,
            compliance_report=compliance_report,
            publishing_record=publishing_record,
            archive_record=archive_record,
            finance_record=finance_record,
            asset_archive_record=asset_archive_record,
            relocation_record=relocation_record,
            production_metadata_record=production_metadata_record,
            analytics_pending_record=analytics_pending_record,
        )

        log.info(
            "pipeline_run_completed",
            episode_id=episode_id,
            status="completed",
            youtube_main_published=(
                publishing_record.get("summary", {}).get("youtube_main_published", False)
            ),
        )

        return result

    except PipelineAuthorizationRequiredError as _exc:
        # The next stage requires authorization that does not currently
        # exist (see _stage_requires_unavailable_authorization() and the
        # exception's own docstring) -- structurally distinct from operator
        # cancellation below. This is a resumable, non-terminal condition:
        # checkpoints on disk are preserved. 'paused' is the existing
        # canonical state for this ("Transition a running run to paused
        # state" -- pause_run()'s own docstring); transitioning there
        # directly (rather than via pause_run(), which is operator-only)
        # keeps this a system-triggered pause without inventing a new state.
        # Only reachable when the caller explicitly passed
        # enforce_authorization_boundaries=True (see run_pipeline()'s own
        # docstring) -- never for any existing production caller today.
        if run_id is not None:
            try:
                transition_run_state(
                    run_id=run_id,
                    to_state="paused",
                    triggered_by="system:authorization_boundary",
                    trigger_source="pipeline",
                    reason=f"paused_before_paid_boundary:{_exc.next_stage}",
                    metadata={
                        "stage_after": _exc.stage_after,
                        "next_stage": _exc.next_stage,
                        "required_authorization_identity": _exc.required_authorization_identity,
                    },
                    db_path=db_path,
                    episode_id=episode_id,
                )
            except Exception as _te:
                log.warn(
                    "pipeline_pause_transition_failed",
                    run_id=run_id,
                    stage_after=_exc.stage_after,
                    error=str(_te)[:200],
                )
        if _current_stage_id is not None:
            _record_stage_failed(
                _current_stage_id, _current_stage_t0,
                f"paused: authorization required for {_exc.next_stage}", log, db_path=db_path,
            )
        try:
            clear_run_context()
        except Exception:
            pass
        log.info(
            "pipeline_run_paused",
            episode_id=episode_id,
            stage_after=_exc.stage_after,
            next_stage=_exc.next_stage,
        )
        if run_id is not None:
            try:
                from hypogeum.run_registry import release_run_lease
                release_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        raise

    except PipelineCancelledError as _exc:
        # An operator explicitly requested cancellation via cancel_check
        # (the only real production meaning of this exception -- see its own
        # docstring; hypogeum.queue.process_next()'s _cancel_check() polls a
        # genuine operator-set cancel flag). Terminal: 'cancelled'
        # (_VALID_TRANSITIONS["cancelled"] == frozenset()) is correct here,
        # unlike the resumable authorization-boundary case handled above.
        if run_id is not None:
            try:
                cancel_run(run_id, episode_id, db_path=db_path)
            except Exception:
                pass
        if _current_stage_id is not None:
            _record_stage_failed(
                _current_stage_id, _current_stage_t0,
                f"cancelled: {_exc.stage_after}", log, db_path=db_path,
            )
        try:
            clear_run_context()
        except Exception:
            pass
        log.info(
            "pipeline_run_cancelled",
            episode_id=episode_id,
            stage_after=_exc.stage_after,
        )
        if run_id is not None:
            try:
                from hypogeum.run_registry import release_run_lease
                release_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        raise

    except Exception as _exc:
        if run_id is not None:
            try:
                fail_run(run_id, episode_id, error_message=str(_exc)[:4096], db_path=db_path)
            except Exception:
                pass
        if _current_stage_id is not None:
            _record_stage_failed(
                _current_stage_id, _current_stage_t0, str(_exc)[:4096], log,
                db_path=db_path,
            )
        try:
            clear_run_context()
        except Exception:
            pass
        if run_id is not None:
            try:
                from hypogeum.run_registry import release_run_lease
                release_run_lease(run_id, db_path=db_path)
            except Exception:
                pass
        raise
    finally:
        try:
            _channel_identity._ACTIVE_CHANNEL.reset(_channel_id_token)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P3-01 Audit persistence helpers
#
# These three functions wrap the DB audit calls for stage_executions.
# All are non-blocking: any exception is caught, logged as a warning,
# and execution continues. A DB write failure must never crash the pipeline.
# ---------------------------------------------------------------------------

def _record_stage_start(
    run_id: str,
    episode_id: str,
    stage_name: str,
    log,
    model_slug: Optional[str] = None,
    output_schema: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Tuple[str, str]:
    """
    Insert a stage_executions row with status='running'.

    Returns (stage_exec_id, started_at_iso). Both are always valid strings
    even when the DB write fails — the caller uses them with _record_stage_*
    functions, which also suppress any DB errors.
    """
    stage_exec_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_connection(db_path)
        try:
            insert_stage_execution(
                conn, stage_exec_id, run_id, episode_id, stage_name,
                started_at,
                model_slug=model_slug,
                output_schema=output_schema,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as _exc:
        log.warn(
            "audit_stage_write_failed",
            stage=stage_name, phase="start", error=str(_exc)[:200],
        )
    return stage_exec_id, started_at


def _record_stage_complete(
    stage_exec_id: str,
    started_at: str,
    log,
    cost_usd: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Update stage_executions row to status='completed'. Non-blocking."""
    try:
        completed_at = datetime.now(timezone.utc).isoformat()
        t0 = datetime.fromisoformat(started_at)
        duration_ms = max(0, int(
            (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        ))
        conn = get_connection(db_path)
        try:
            update_stage_execution_complete(
                conn, stage_exec_id, completed_at,
                duration_ms=duration_ms, cost_usd=cost_usd,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as _exc:
        log.warn(
            "audit_stage_write_failed",
            stage_exec_id=stage_exec_id, phase="complete", error=str(_exc)[:200],
        )


def _record_stage_failed(
    stage_exec_id: str,
    started_at: str,
    error_message: str,
    log,
    db_path: Optional[Path] = None,
) -> None:
    """Update stage_executions row to status='failed'. Non-blocking."""
    try:
        completed_at = datetime.now(timezone.utc).isoformat()
        t0 = datetime.fromisoformat(started_at)
        duration_ms = max(0, int(
            (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        ))
        conn = get_connection(db_path)
        try:
            update_stage_execution_failed(
                conn, stage_exec_id, completed_at,
                error_message[:4096], duration_ms=duration_ms,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as _exc:
        log.warn(
            "audit_stage_write_failed",
            stage_exec_id=stage_exec_id, phase="failed", error=str(_exc)[:200],
        )


def _record_stage_skipped(
    stage_exec_id: str,
    log,
    db_path: Optional[Path] = None,
) -> None:
    """Update stage_executions row to status='skipped' (checkpoint resume). Non-blocking."""
    try:
        completed_at = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db_path)
        try:
            update_stage_execution_skipped(conn, stage_exec_id, completed_at)
            conn.commit()
        finally:
            conn.close()
    except Exception as _exc:
        log.warn(
            "audit_stage_write_failed",
            stage_exec_id=stage_exec_id, phase="skipped", error=str(_exc)[:200],
        )


# ---------------------------------------------------------------------------
# Agent stubs
#
# Each stub is the integration point for one agent. Stubs raise
# NotImplementedError until implemented in M1–M5. The pipeline calls them via
# the names below so tests can mock individual agents independently.
#
# Model assignments (current — see .env for overrides):
#   EP → claude-sonnet-4.6 (config.EP_MODEL)
#   RD → claude-opus-4.6 (config.RD_MODEL)
#   TD planning → openai/gpt-5.5-pro (config.TD_PLANNING_MODEL)
#   BGA Pass 1 → claude-sonnet-4.6 (config.BGA_PASS1_MODEL)
#   BGA Pass 2 → openai/gpt-5.5-pro (config.BGA_PASS2_MODEL)
#   SE, VPD, Audio, Assembly, Publishing → claude-sonnet-4.6
#
# Each stub documents its expected return type and which milestone implements it.
# ---------------------------------------------------------------------------

def _run_executive_producer(
    topic_input: str,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> tuple[dict, dict]:
    """
    Executive Producer (M1.2 — implemented).

    Delegates to hypogeum.agents.executive_producer.run_executive_producer().
    Calls run_agent() twice: first for validation_scorecard, then (if PASS/WARN)
    for topic_brief. Returns (scorecard, brief); brief is {} on REJECT.

    Model: Sonnet 4.6
    Max tokens: 4,096 (per call)
    Output schemas: validation_scorecard, topic_brief

    Checkpoint: loads existing outputs from disk if both files are present.
    """
    scorecard = _load_package(episode_id, "validation_scorecard", output_dir)
    brief     = _load_package(episode_id, "topic_brief", output_dir)
    if scorecard is not None and brief is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info("pipeline_stage_skipped", agent="executive_producer", reason="existing_output")
        return scorecard, brief
    return run_executive_producer(
        topic_input=topic_input,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_research_director(
    topic_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> tuple[dict, Optional[dict]]:
    """
    Research Director (M1.3 — implemented).

    Delegates to hypogeum.agents.research_director.run_research_director().
    Runs Perplexity discovery queries then Claude Opus synthesis.
    Returns (research_package, None) on success, ({}, pdr) when PDR is issued.

    Model: Sonnet 4.6
    Max tokens: 16,384
    Output schemas: research_package (+ optional package_deficiency_report)

    Checkpoint: loads existing research_package from disk if present.
    """
    pkg = _load_package(episode_id, "research_package", output_dir)
    if pkg is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info("pipeline_stage_skipped", agent="research_director", reason="existing_output")
        return pkg, None
    return run_research_director(
        topic_brief=topic_brief,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_narrative_architect(
    research_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> dict:
    """
    Narrative Architect (M1.4 — implemented).

    Delegates to hypogeum.agents.narrative_architect.run_narrative_architect().
    Consumes research_package and produces narrative_package via Claude Sonnet.

    Model: Sonnet 4.6
    Max tokens: 16,384
    Output schema: narrative_package

    Checkpoint: loads existing narrative_package from disk if present.
    """
    pkg = _load_package(episode_id, "narrative_package", output_dir)
    if pkg is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info("pipeline_stage_skipped", agent="narrative_architect", reason="existing_output")
        return pkg
    return run_narrative_architect(
        research_package=research_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_script_engine(
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> dict:
    """
    Script Engine (M1.5 — implemented).

    Delegates to hypogeum.agents.script_engine.run_script_engine().
    Consumes narrative_package and produces script_package via Claude Sonnet.
    Writes narration beats for all sections and all 3 Shorts, CTAs, chapter
    markers, text overlays, and shorts extraction markers.

    Model: Sonnet 4.6
    Max tokens: 16,384
    Output schema: script_package

    Checkpoint: loads existing script_package from disk if present.
    """
    pkg = _load_package(episode_id, "script_package", output_dir)
    if pkg is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info("pipeline_stage_skipped", agent="script_engine", reason="existing_output")
        return pkg
    return run_script_engine(
        narrative_package=narrative_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_thumbnail_strategy(
    topic_brief: dict,
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
) -> dict:
    """
    Thumbnail Strategy (wired into the pipeline 2026-07-19 — completing an
    already-designed architecture, per hypogeum/agents/thumbnail_strategy.py's
    own docstring: "Architecture position: topic_brief + script_package ->
    [THIS SKILL] -> thumbnail_concept_brief; thumbnail_concept_brief ->
    (future) VPD + TD." Previously reachable only via the standalone
    scripts/generate_thumbnail_strategy.py — see
    docs/architecture/THUMBNAIL_CONCEPT_MODE_CONTEXT_AUDIT.md for the full
    prior-state audit (2026-07-11) that first identified this disconnect:
    0 of ~55 real local episodes had ever exercised concept-driven mode via
    the automated pipeline, while hypogeum.agents.thumbnail_qa.py's own
    existing regression test asserts legacy-mode thumbnails must fail QA.

    Delegates to hypogeum.agents.thumbnail_strategy.run_thumbnail_strategy().
    Consumes topic_brief + script_package and produces thumbnail_concept_brief
    via Claude Sonnet. Writing it to output_dir as
    thumbnail_concept_brief_{episode_id}.json is what activates
    CONCEPT_DRIVEN_MODE downstream — hypogeum.agents.thumbnail_director.py's
    own existing file-existence check picks it up automatically; no change
    to that file's own logic was needed or made.

    Model: Sonnet 4.6
    Max tokens: 1,500
    Output schema: thumbnail_concept_brief

    Checkpoint: loads existing thumbnail_concept_brief from disk if present.
    """
    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        # A vertical Short has no concept-driven thumbnail (the platform
        # auto-generates a cover from a frame). Do NOT write a
        # thumbnail_concept_brief file -- its absence is exactly what keeps
        # CONCEPT_DRIVEN_MODE off downstream. Return a minimal in-memory
        # shape purely for the caller's log line.
        get_logger(episode_id=episode_id, agent="pipeline").info(
            "pipeline_stage_skipped", agent="thumbnail_strategy", reason="short_native_mode",
        )
        return {"thumbnail_concept_brief_id": f"tcb_{episode_id}",
                "episode_id": episode_id, "concepts": [], "short_native": True}

    pkg = _load_package(episode_id, "thumbnail_concept_brief", output_dir)
    if pkg is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info("pipeline_stage_skipped", agent="thumbnail_strategy", reason="existing_output")
        return pkg
    return run_thumbnail_strategy(
        topic_brief=topic_brief,
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )


def _run_visual_package_director(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    *,
    narration_package: Optional[dict] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Visual Package Director (M2.3/M2.4 — implemented).

    Phase 1: Claude Sonnet 4.6 plans scenes, entity lock states, and thumbnail
    candidates. Phase 2: MUAPI generates reference images (final-locked entities)
    then all scene videos. Also extracts and writes Thumbnail Handoff Brief (M2.4).

    narration_package: validated Voice Asset output, now available because
    Audio Agent runs before Visual Package Director in the canonical fresh
    workflow — consumed by Cinematic Coverage Planning inside Phase 1 for
    measured per-beat narration durations. Keyword-only and optional
    (defaults to {}) so existing direct callers/tests that don't yet pass it
    keep working, falling back to Cinematic Director's own per-beat
    word-count estimate exactly as before. Only reaches the fresh-plan
    branch below; run_visual_package_director_from_plan() resumes Phase 2
    only, after Phase 1 (including Cinematic Director) already completed,
    so it has no use for it.

    Model: Sonnet 4.6
    Max tokens: 32,768
    Output schema: visual_package

    Checkpoint: three paths based on disk state:
      1. No visual_package on disk → run Phase 1 (planning) + Phase 2 (MUAPI).
      2. visual_package on disk, all assets terminal-success → skip entirely.
      3. visual_package on disk, some assets failed or missing → skip Phase 1,
         resume Phase 2 (submit_scene skips already-downloaded clips via file check).

    Phase 2 is complete only when ALL assets are in a terminal successful state
    (generated / reused / dry_run). A mix of generated + generation_failed means
    Phase 2 must resume to re-attempt the failed scenes.
    """
    existing_plan = _load_package(episode_id, "visual_package", output_dir)
    if existing_plan is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        assets = existing_plan.get("asset_manifest", {}).get("assets", [])
        # ALL assets must be terminal-successful to skip Phase 2 entirely.
        # Any generation_failed means submit_scene must retry those scenes.
        _TERMINAL_SUCCESS = {"generated", "reused", "dry_run", "deferred"}

        def _asset_is_terminal_success(a: dict) -> bool:
            if a.get("generation_status") in _TERMINAL_SUCCESS:
                return True
            # clip_repair_agent's coverage-fill path appends a contingency
            # ("__extraN") asset to the manifest ONLY after the clip has
            # passed qualify_asset() -- and that entry carries no
            # generation_status key at all (it records model/tier_used/
            # planned_shot_id instead). Treat such an entry as the terminal
            # success it structurally is: a bare generation_status=None is a
            # coverage-fill extra, not a failed primary, when it names a real
            # asset_id and the planned shot it came from. Without this, an
            # episode whose Phase 2 fully completed (every primary generated,
            # coverage filled) is misread as incomplete on resume, needlessly
            # re-running Cinematic Director -- which can regenerate the
            # coverage plan and balloon the compiled Production Plan's cost
            # bound past the finance ceiling for an episode that has nothing
            # left to generate.
            return (
                a.get("generation_status") is None
                and bool(a.get("asset_id"))
                and bool(a.get("planned_shot_id"))
            )

        phase2_done = bool(assets) and all(
            _asset_is_terminal_success(a) for a in assets
        )
        if phase2_done:
            log.info(
                "pipeline_stage_skipped",
                agent="visual_package_director",
                reason="phase2_complete",
                assets_total=len(assets),
            )
            return existing_plan
        failed_count = sum(
            1 for a in assets
            if not _asset_is_terminal_success(a)
        )
        log.info(
            "pipeline_stage_skipped",
            agent="visual_package_director_phase1",
            reason="existing_plan",
            assets_total=len(assets),
            assets_to_retry=failed_count,
        )
        return run_visual_package_director_from_plan(
            visual_package=existing_plan,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
            script_package=script_package,
            narration_package=narration_package or {},
        )
    return run_visual_package_director(
        script_package=script_package,
        narration_package=narration_package or {},
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_source_enhancement(
    visual_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> dict:
    """
    Source Enhancement (2026-08-02). Provider-agnostic stage between Source
    Generation (Visual Package Director Phase 2, above) and Coverage: raises
    any source clip below the required source resolution via the configured
    Enhancement Provider (see hypogeum/source_enhancement.py's own docstring
    for the full pipeline position and fail-closed contract). A clip already
    at or above the required resolution is reused untouched, at no cost.
    """
    from hypogeum.source_enhancement import run_source_enhancement  # noqa: PLC0415

    return run_source_enhancement(
        visual_package=visual_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _short_native_thumbnail_package(
    episode_id: str, output_dir: Optional[Path], log,
) -> dict:
    """
    SHORT_NATIVE mode: a vertical Short has no concept-driven thumbnail --
    the platform auto-generates a cover from a frame. Produce a minimal,
    schema-valid thumbnail_package (persisted to disk like a real agent
    output) so downstream checkpoint/QA/output-gate reads resolve, and the
    concept-driven thumbnail stack is never invoked.
    """
    from hypogeum.agents.thumbnail_director import _AGENT_VERSION as _td_version  # noqa: PLC0415

    pkg = {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {"asset_id": "short_native_autocover"},
        "short_thumbnails": [],
        "candidates": [
            {"candidate_id": "sn_auto_001", "rank": 1, "selected": True, "status": "auto_cover"}
        ],
        "thumbnail_qa_report": {"overall_status": "pass", "failures": [], "warnings": []},
        "generation_metadata": {
            "concept_driven": False,
            "short_native": True,
            "thumbnail_director_version": _td_version,
        },
        "package_integrity": {"production_ready": True},
    }
    try:
        from hypogeum.schemas import validate
        validate("thumbnail_package", pkg)
    except Exception as exc:  # pragma: no cover - schema drift guard
        log.warn("short_native_thumbnail_package_schema_warn", error=str(exc))

    base = output_dir if output_dir is not None else (_ASSETS_ROOT / "episodes" / episode_id)
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / f"thumbnail_package_{episode_id}.json").write_text(
            json.dumps(pkg, indent=2), encoding="utf-8",
        )
    except Exception as exc:
        log.warn("short_native_thumbnail_package_write_failed", error=str(exc))

    log.info("pipeline_stage_skipped", agent="thumbnail_director", reason="short_native_mode")
    return pkg


def _run_thumbnail_director(
    visual_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    channel_id: Optional[str] = None,
) -> dict:
    """
    Thumbnail Director (M2.5 — implemented).

    Delegates to hypogeum.agents.thumbnail_director.run_thumbnail_director().
    TD accepts only the Thumbnail Handoff Brief — never the full Visual Package.
    The brief is extracted from the visual_package dict via
    build_thumbnail_handoff_brief() before being passed to run_thumbnail_director().

    Model: Sonnet 4.6
    Max tokens: 4,096
    Output schema: thumbnail_package

    Checkpoint: loads existing thumbnail_package from disk if present,
    production_ready, has every referenced thumbnail file on disk, AND was
    produced by the current thumbnail_director policy version (see
    _THUMBNAIL_CHECKPOINT / _thumbnail_checkpoint_compatible — 2026-08-02
    artifact-compatibility policy; a stale package is never reused by
    presence alone).
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")

    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        return _short_native_thumbnail_package(episode_id, output_dir, log)

    pkg = _load_verified_checkpoint(
        episode_id, output_dir, _THUMBNAIL_CHECKPOINT, log,
    )
    if pkg is not None:
        log.info("pipeline_stage_skipped", agent="thumbnail_director", reason="existing_output")
        return pkg
    _resolve_output_dir = output_dir or (
        Path(__file__).resolve().parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    handoff_brief = build_thumbnail_handoff_brief(
        visual_package=visual_package,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
    )
    return run_thumbnail_director(
        handoff_brief=handoff_brief,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        channel_id=channel_id,
    )


def _run_audio_agent(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    skip_shorts: bool = False,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Audio Agent (M3.2–M3.6 — implemented).

    Delegates to hypogeum.agents.audio_agent.run_audio_agent().
    Orchestrates ElevenLabs beat-level narration generation, MP3 stitching,
    SRT caption generation (with Whisper fallback), and seals the Narration Package.

    No Claude calls — pure Python orchestration over elevenlabs.py.

    Max tokens: 2,048 (not applicable — no LLM call)
    Output schema: narration_package

    Checkpoint: loads existing narration_package from disk if present,
    production_ready, and every referenced audio asset file still exists.
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")
    pkg = _load_verified_checkpoint(
        episode_id, output_dir, _NARRATION_CHECKPOINT, log,
    )
    if pkg is not None:
        log.info("pipeline_stage_skipped", agent="audio_agent", reason="existing_output")
        return pkg
    return run_audio_agent(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        skip_shorts=skip_shorts,
        channel_id=channel_id,
    )


def _run_assembly_agent(
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    script_package: Optional[dict] = None,
    source_registry: Optional[list] = None,
    topic_brief: Optional[dict] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Assembly Agent (M4.2–M4.8 — implemented).

    Delegates to hypogeum.agents.assembly_agent.run_assembly_agent().
    Validates asset inventory, constructs narration-anchored timeline,
    sources music (library-first, ElevenLabs Music on cache miss), mixes
    audio (narration 0dB + music −18dB), renders main episode (16:9) and
    3 Shorts (9:16) via ffmpeg, and produces the Final Render Package.

    No Claude calls — pure Python orchestration over elevenlabs.py and ffmpeg.

    Max tokens: 4,096 (not applicable — no LLM call)
    Output schema: final_render_package

    Checkpoint: loads existing final_render_package from disk if present,
    production_ready, and every referenced video asset file still exists.
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")
    pkg = _load_verified_checkpoint(
        episode_id, output_dir, _ASSEMBLY_CHECKPOINT, log,
    )
    if pkg is not None:
        log.info("pipeline_stage_skipped", agent="assembly_agent", reason="existing_output")
        return pkg
    return run_assembly_agent(
        visual_package=visual_package,
        narration_package=narration_package,
        thumbnail_package=thumbnail_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        script_package=script_package,
        source_registry=source_registry,
        topic_brief=topic_brief,
        channel_id=channel_id,
    )


def _run_archive_agent(
    final_render_package: dict,
    publishing_record: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
) -> dict:
    """
    Archive Agent (R49 — implemented).

    Delegates to hypogeum.agents.archive_agent.run_archive_agent().
    Validates publishing outcomes and assembles a durable episode_summary
    from the final_render_package and publishing_record. Non-blocking —
    archive outcome is logged and stored in PipelineResult but does not
    raise exceptions; the episode is already published.

    No Claude calls — pure deterministic evaluation.

    Output schema: archive_record
    """
    return run_archive_agent(
        final_render_package=final_render_package,
        publishing_record=publishing_record,
        episode_id=episode_id,
        output_dir=output_dir,
    )


def _run_archive_relocation(
    episode_id: str,
    channel_id: Optional[str],
    output_dir: Optional[Path],
    db_path: Optional[Path],
) -> dict:
    """
    Archive Relocation — terminal, non-blocking.

    Moves the published episode's asset tree from the working
    ``.hypogeum-assets/episodes/<episode_id>/`` directory to the configured
    archive root and repoints the catalog (archive_index + JSONL + assets).
    Only acts on an episode with a succeeded publication; otherwise a no-op.
    Never raises — the episode is already published and catalogued.

    Delegates to hypogeum.archive_relocation.relocate_episode_assets().
    """
    from hypogeum.archive_relocation import relocate_episode_assets
    return relocate_episode_assets(
        episode_id,
        channel_id or "hypogeum",
        db_path=db_path,
        output_dir=output_dir,
    )


def _run_compliance_agent(
    final_render_package: dict,
    source_registry: list,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
) -> dict:
    """
    Rights & Compliance Agent (R48 — implemented).

    Delegates to hypogeum.agents.compliance_agent.run_compliance_agent().
    Validates AI disclosure, source attribution, music/license disclosure,
    and platform metadata requirements. Emits compliance_report with verdict
    APPROVED, WARNING, or REJECTED. REJECTED blocks Publishing Agent via
    ComplianceRejectedError.

    No Claude calls — pure deterministic evaluation.

    Output schema: compliance_report
    """
    return run_compliance_agent(
        final_render_package=final_render_package,
        episode_id=episode_id,
        source_registry=source_registry,
        output_dir=output_dir,
    )


def _run_qa_agent(
    final_render_package: dict,
    thumbnail_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
) -> dict:
    """
    QA Agent (R47 — implemented).

    Delegates to hypogeum.agents.qa_agent.run_qa_agent().
    Evaluates final_render_package, render_review_verdict (if available),
    audio_qa_verdict (if available), and thumbnail_package. Emits a
    qa_approval_report with verdict APPROVED, WARNING, or REJECTED.
    REJECTED blocks the Publishing Agent via QARejectedError.

    No Claude calls — pure deterministic evaluation.

    Output schema: qa_approval_report
    """
    return run_qa_agent(
        final_render_package=final_render_package,
        episode_id=episode_id,
        thumbnail_package=thumbnail_package,
        output_dir=output_dir,
    )


def _run_budget_authorization(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    db_path: Optional[Path] = None,
) -> dict:
    """
    Finance Agent — budget authorization gate (R50 — implemented).

    Delegates to hypogeum.agents.finance_agent.run_budget_authorization().
    Estimates planned spend from script_package and compares against
    FINANCE_BUDGET_CEILING_USD. Returns finance_record with phase="authorization".
    BLOCKED verdict raises FinanceBudgetBlockedError before any provider spend.

    No Claude calls — pure deterministic evaluation.

    Output schema: finance_record (phase="authorization")
    """
    return run_budget_authorization(
        script_package=script_package,
        episode_id=episode_id,
        config=config,
        output_dir=output_dir,
        db_path=db_path,
    )


def _run_cost_ledger(
    final_render_package: dict,
    publishing_record: dict,
    archive_record: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    narration_package: Optional[dict] = None,
    visual_package: Optional[dict] = None,
    run_id: Optional[str] = None,
) -> dict:
    """
    Finance Agent — post-run cost ledger (R50 — implemented).

    Delegates to hypogeum.agents.finance_agent.run_cost_ledger().
    Records actual cost signals from pipeline artifacts. Non-blocking —
    ledger outcome is logged and stored in PipelineResult but does not
    raise exceptions; the episode is already published.
    Appends to JSONL ledger if FINANCE_LEDGER_PATH is configured.

    No Claude calls — pure deterministic evaluation.

    Output schema: finance_record (phase="ledger")
    """
    return run_cost_ledger(
        final_render_package=final_render_package,
        publishing_record=publishing_record,
        archive_record=archive_record,
        episode_id=episode_id,
        config=config,
        narration_package=narration_package,
        visual_package=visual_package,
        output_dir=output_dir,
        run_id=run_id,
    )


def _run_asset_archive(
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    final_render_package: dict,
    episode_id: str,
    output_dir: Optional[Path],
    run_id: Optional[str] = None,
) -> dict:
    """
    Asset Archive Agent — catalog all generated assets (Phase 3A).

    Delegates to hypogeum.agents.asset_archive_agent.run_asset_archive().
    Writes to shared JSONL catalogs and the operational DB assets table.
    Non-blocking — verdict is logged but never raises.

    Output schema: asset_archive_record
    """
    return run_asset_archive(
        visual_package=visual_package,
        narration_package=narration_package,
        thumbnail_package=thumbnail_package,
        final_render_package=final_render_package,
        episode_id=episode_id,
        output_dir=output_dir,
        run_id=run_id,
    )


def _get_episode_publish_at(episode_id: str, db_path: Optional[Path]) -> Optional[str]:
    """
    Return target_publish_at from the most recent consumed episode_topics row, or None.

    None means no publish schedule is recorded — the pipeline will block YouTube upload
    rather than default to public.
    """
    try:
        from hypogeum.db import _db_path as _resolve_db_path
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                "SELECT target_publish_at FROM episode_topics "
                "WHERE episode_id = ? AND target_publish_at IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
        finally:
            conn.close()
        return row["target_publish_at"] if row else None
    except Exception:
        return None


def _channel_uses_zernio_transport(
    channel_id: Optional[str],
    publishing_provider_by_platform: Optional[dict],
    db_path: Optional[Path],
) -> bool:
    """
    True when the channel publishes through Zernio rather than the direct
    per-platform clients: any value in publishing_provider_by_platform (the
    explicit override, or the channel's persisted PublishingProfile) is
    "zernio".
    """
    routing = dict(publishing_provider_by_platform or {})
    if not routing and channel_id:
        try:
            from hypogeum.channel_config import load_channel_config
            cc = load_channel_config(channel_id, db_path=db_path)
            if cc is not None and cc.publishing is not None:
                routing = dict(cc.publishing.publishing_provider_by_platform or {})
        except Exception:
            routing = {}
    return any(str(v).strip().lower() == "zernio" for v in routing.values())


def _zernio_bundle_definitions(channel_id: str, db_path: Optional[Path]) -> list:
    """Compile ChannelOperationsProfile.deliverable_bundle_definitions into
    hypogeum.buffer.DeliverableBundleDefinition objects — the same shape
    run_worker._resolve_bundle_definitions_for_channel produces."""
    try:
        from hypogeum.buffer import DeliverableBundleDefinition
        from hypogeum.channel_config import get_channel_operations_profile
        prof = get_channel_operations_profile(channel_id, db_path=db_path)
    except Exception:
        return []
    if prof is None:
        return []
    out = []
    for entry in (getattr(prof, "deliverable_bundle_definitions", None) or []):
        if not isinstance(entry, dict):
            continue
        bid = entry.get("bundle_id")
        target = entry.get("target_buffer_count")
        if not bid or not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            continue
        out.append(DeliverableBundleDefinition(
            channel_id=channel_id, bundle_id=bid, target_buffer_count=target,
        ))
    return out


def _run_zernio_publish(
    channel_id: str,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path],
    db_path: Optional[Path],
    assets_root: Optional[Path],
    publish_at: Optional[str],
    log,
) -> Optional[dict]:
    """
    Publish this channel's ready deliverables cross-platform through the
    Zernio release flow (hypogeum.publishing.zernio_channel_release_trigger.
    process_channel_ready_bundles) -- the SAME idempotent, output-gated,
    per-platform-aggregated path the autonomous worker uses. Returns a
    publishing_record dict, or None if the channel has no bundle
    definition (caller falls back to the direct publishing agent).
    """
    bundle_defs = _zernio_bundle_definitions(channel_id, db_path)
    if not bundle_defs:
        log.warn("pipeline_zernio_no_bundle_definition", channel_id=channel_id,
                 note="channel routes to zernio but has no deliverable_bundle_definitions — using direct publish")
        return None

    from hypogeum.publishing.zernio_channel_release_trigger import process_channel_ready_bundles
    _assets_root = Path(assets_root) if assets_root is not None else _ASSETS_ROOT
    _publish_now = publish_at is None  # a scheduled slot -> Zernio schedules it; none -> publish now

    ready: list = []
    for bdef in bundle_defs:
        ready.extend(process_channel_ready_bundles(
            channel_id, bdef, db_path=db_path, assets_root=_assets_root,
            publish_now=_publish_now, config=config, log=log,
            enable_production_output_gate=True,
        ))

    # Keep only entries for THIS episode (item_id is the episode identity).
    mine = [r for r in ready if getattr(r, "item_id", None) == episode_id] or ready

    from hypogeum.publishing.zernio_channel_release_trigger import _status_for_platform
    platform_results = []
    published = failed = skipped = 0
    yt_main_published = False
    for i, r in enumerate(mine):
        try:
            raw = _status_for_platform(r.result, r.platform)
        except Exception:
            raw = getattr(r.result, "status", "failed")
        status = "published" if raw == "published" else (
            "skipped" if str(raw).startswith("blocked") else "failed")
        if status == "published":
            published += 1
            if r.platform == "youtube":
                yt_main_published = True
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
        platform_results.append({
            "result_id": f"zres_{episode_id}_{i}",
            "platform": r.platform,
            "deliverable_type": "main_episode",
            "status": status,
            "metadata_applied": {"ai_disclosure_included": True},
        })

    if not platform_results:
        platform_results.append({
            "result_id": f"zres_{episode_id}_none", "platform": "youtube",
            "deliverable_type": "main_episode", "status": "failed",
            "metadata_applied": {"ai_disclosure_included": True},
        })
        failed = 1

    overall = ("published" if failed == 0 and published > 0
               else "partially_published" if published > 0
               else "failed")

    record = {
        "package_metadata": {
            "publishing_record_id": f"pub_{episode_id}_zernio",
            "episode_id": episode_id,
            "overall_status": overall,
        },
        "platform_results": platform_results,
        "failures": [],
        "summary": {
            "youtube_main_published": yt_main_published,
            "total_deliverables_attempted": len(platform_results),
            "total_published": published,
            "total_failed": failed,
            "total_skipped": skipped,
        },
        "package_integrity": {"production_ready": overall == "published"},
        "transport": "zernio",
    }
    try:
        from hypogeum.schemas import validate
        validate("publishing_record", record)
    except Exception as exc:  # pragma: no cover - schema drift guard
        log.warn("pipeline_zernio_record_schema_warn", error=str(exc))

    base = output_dir if output_dir is not None else (_ASSETS_ROOT / "episodes" / episode_id)
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        (Path(base) / f"publishing_record_{episode_id}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warn("pipeline_zernio_record_write_failed", error=str(exc))

    log.info("pipeline_zernio_publish_complete", channel_id=channel_id,
             overall_status=overall, published=published, failed=failed, skipped=skipped)
    return record


def _run_publishing_agent(
    final_render_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    youtube_privacy: str = "public",
    publish_at: Optional[str] = None,
    enabled_platforms: Optional[frozenset] = None,
    publishing_provider_by_platform: Optional[dict] = None,
    channel_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    assets_root: Optional[Path] = None,
) -> dict:
    """
    Publishing Agent (M5.2 — implemented).

    Delegates to hypogeum.agents.publishing_agent.run_publishing_agent().
    Distributes the main episode (16:9) and all 3 Shorts (9:16) to YouTube,
    Instagram Reels, Facebook Video, and TikTok (if available). Applies
    platform-specific metadata, AI disclosure text, and retry policy.

    enabled_platforms: forwarded unchanged to run_publishing_agent()'s own
    enabled_platforms parameter — see that function's docstring for the
    exact per-platform gating contract. None preserves prior behavior.

    publishing_provider_by_platform: forwarded unchanged to
    run_publishing_agent()'s own parameter of the same name (ADR-013
    Decision 7, item 1). None (default, every existing caller) preserves
    prior behavior exactly.

    No Claude calls — pure Python orchestration over platform client modules.

    Max tokens: 2,048 (not applicable — no LLM call)
    Output schema: publishing_record

    Checkpoint: loads existing publishing_record from disk if present and
    overall_status == "published" — skips re-entering this agent entirely
    (OAuth refresh, pre-flight validation, all platform attempts).
    "partially_published" and "failed" always re-run. Complementary to,
    not a replacement for, the deliverable-level publishing_outcomes guard
    inside run_publishing_agent() itself, which still protects the YouTube
    main upload specifically even if this checkpoint is ever bypassed.
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")
    pkg = _load_verified_checkpoint(
        episode_id, output_dir, _PUBLISHING_CHECKPOINT, log,
    )
    if pkg is not None:
        log.info("pipeline_stage_skipped", agent="publishing_agent", reason="already_published")
        return pkg

    # Zernio transport: a channel whose PublishingProfile routes platforms to
    # "zernio" publishes cross-platform through the same idempotent,
    # output-gated release flow the worker uses -- not the direct per-platform
    # clients. (Root-cause fix 2026-08-29: previously the pipeline only ever
    # did YouTube-direct and cross-platform needed a separate manual step.)
    if _channel_uses_zernio_transport(channel_id, publishing_provider_by_platform, db_path):
        zrec = _run_zernio_publish(
            channel_id=channel_id or "hypogeum", episode_id=episode_id, config=config,
            output_dir=output_dir, db_path=db_path, assets_root=assets_root,
            publish_at=publish_at, log=log,
        )
        if zrec is not None:
            return zrec
        # fall through to direct publish when the channel has no bundle def

    return run_publishing_agent(
        final_render_package=final_render_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        youtube_privacy=youtube_privacy,
        publish_at=publish_at,
        enabled_platforms=enabled_platforms,
        publishing_provider_by_platform=publishing_provider_by_platform,
    )
