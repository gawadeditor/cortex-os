"""
hypogeum/pipeline.py — Pipeline orchestrator

Single run_pipeline() entry point that executes all 9 agents in the frozen order.
This is a scaffold — every agent stub raises NotImplementedError until implemented
in M1–M5. The orchestration flow, lifecycle logging, and fail-fast checks are complete.

Agent order (frozen — architecture-index.md):
  EP → RD → NA → SE → VPD → TD → Audio → Assembly → Publishing

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
  8.  Visual Package Director → visual_package
  9.  Thumbnail Director → thumbnail_package
  10. Audio Agent → narration_package
  11. Assembly Agent → final_render_package
  12. Publishing Agent → publishing_record
  13. Log pipeline_run_completed; return PipelineResult

Logging events emitted:
  pipeline_run_started         — info  — before startup checks
  pipeline_startup_verified    — info  — after startup checks pass
  pipeline_stage_started       — info  — before each agent call
  pipeline_stage_completed     — info  — after each agent returns
  pipeline_topic_rejected      — warn  — EP scored topic REJECT
  pipeline_pdr_issued          — error — RD issued a Package Deficiency Report
  pipeline_run_completed       — info  — all 9 agents completed
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from hypogeum.agents.asset_archive_agent import run_asset_archive
from hypogeum.agents.assembly_agent import run_assembly_agent
from hypogeum.agents.audio_agent import run_audio_agent
from hypogeum.agents.executive_producer import run_executive_producer
from hypogeum.agents.archive_agent import run_archive_agent
from hypogeum.agents.humanization_stage import (
    HumanizationStageError,
    run_humanization_stage,
)
from hypogeum.agents.humanizer_llm_adapter import HUMANIZER_LLM_MODEL_ID
from hypogeum.agents.compliance_agent import run_compliance_agent
from hypogeum.agents.finance_agent import run_budget_authorization, run_cost_ledger
from hypogeum.agents.qa_agent import run_qa_agent
from hypogeum.agents.narrative_architect import run_narrative_architect
from hypogeum.agents.publishing_agent import run_publishing_agent
from hypogeum.agents.research_director import run_research_director
from hypogeum.agents.script_engine import run_script_engine
from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
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
from hypogeum.finance import clear_run_context, set_run_context, write_episode_rollup
from hypogeum.humanization_checkpoint import (
    load_reusable_humanization_checkpoint,
    save_humanization_checkpoint,
)
from hypogeum.ois import OISBlockedError, get_blocking_signals, is_channel_blocked, is_factory_blocked
from hypogeum.post_run_intelligence import run_post_run_intelligence
from hypogeum.production_metadata import write_analytics_pending, write_production_metadata
from hypogeum.logger import get_logger
from hypogeum.run_registry import cancel_run, complete_run, fail_run, register_run, transition_run_state
from hypogeum.startup import StartupReport, run_startup_checks


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


class ChannelNotFoundError(PipelineError):
    """
    No channel record exists in the DB for the requested channel_id.

    Run scripts/bootstrap_channel.py to create the channel before
    invoking the pipeline with --channel-id.

    Attributes:
        channel_id — the channel_id that was not found.
    """
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
    humanized_script_package: Optional[dict] = None
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
    production_metadata_record: Optional[dict] = None
    analytics_pending_record: Optional[dict] = None
    approval_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    topic_input: str,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
    youtube_privacy: str = "public",
    channel_id: Optional[str] = "hypogeum",
    db_path: Optional[Path] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> PipelineResult:
    """
    Execute the full 9-agent pipeline end-to-end.

    Args:
        topic_input:      The topic idea string passed to the Executive Producer.
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
    try:
        run_id = register_run(episode_id=episode_id, config=config, channel_id=channel_id)
        set_run_context(run_id=run_id, episode_id=episode_id)
        log.info("pipeline_run_registered", run_id=run_id, channel_id=channel_id)

        # -----------------------------------------------------------------------
        # 1. Startup verification — fail-fast before any agent is called
        # -----------------------------------------------------------------------
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "startup_checks", log,
            db_path=db_path,
        )
        startup_report = run_startup_checks(config, episode_id=episode_id)
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
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

        # -----------------------------------------------------------------------
        # 3. Research Director
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="research_director")
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
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

        # -----------------------------------------------------------------------
        # 4. Narrative Architect
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="narrative_architect")
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="narrative_architect")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="narrative_architect")

        # -----------------------------------------------------------------------
        # 5. Script Engine
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="script_engine")
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="script_engine")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="script_engine")

        # -----------------------------------------------------------------------
        # 5b. Humanization Stage — SHADOW mode (hardcoded; mode name unchanged
        # from the prior task — the schema/implementation don't yet support
        # ACTIVE, per docs/architecture/HUMANIZER_SHADOW_RUNTIME_AUDIT.md).
        # Produces humanized_script_package alongside script_package without
        # mutating or replacing either. Runs the pinned upstream Humanizer
        # LLM pass for real. Its VALIDATED output is now production-consumed:
        # immediately below, effective_script_package merges every accepted
        # beat (transformation_status not "reverted"/"failed") into a
        # schema-valid script_package shape, which Finance authorization,
        # VPD, Audio, and Assembly receive instead of the raw script_package.
        # Reverted/failed beats keep their original narration in the merge.
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="humanization_stage")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "humanization_stage", log,
            output_schema="humanized_script_package",
            db_path=db_path,
        )
        humanized_script_package = _run_humanization_stage(
            episode_id=episode_id,
            script_package=script_package,
            pipeline_run_id=run_id,
            output_dir=output_dir,
            config=config,
            log=log,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="humanization_stage")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="humanization_stage")

        # effective_script_package: the validated Humanizer-applied package,
        # or a content-identical copy of the original when Humanization
        # could not safely provide one (full fallback). script_package
        # itself is never reassigned — it remains the original, unmutated
        # Script Engine output, carried on PipelineResult for provenance,
        # audit, comparison, and rollback.
        effective_script_package = _build_effective_script_package(
            script_package, humanized_script_package,
        )

        # -----------------------------------------------------------------------
        # 6. Finance Agent — budget authorization gate (pre-generation spend)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="finance_agent_authorization")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "finance_agent_authorization", log,
            output_schema="finance_authorization",
            db_path=db_path,
        )
        finance_authorization = _run_budget_authorization(
            script_package=effective_script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
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

        # -----------------------------------------------------------------------
        # 7. Visual Package Director
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="visual_package_director")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "visual_package_director", log,
            output_schema="visual_package",
            db_path=db_path,
        )
        visual_package = _run_visual_package_director(
            script_package=effective_script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="visual_package_director")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="visual_package_director")

        # -----------------------------------------------------------------------
        # 7b. Thumbnail Director
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="thumbnail_director")
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="thumbnail_director")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="thumbnail_director")

        # -----------------------------------------------------------------------
        # 8. Audio Agent (takes Script Package — parallel path to VPD+TD in M3)
        # -----------------------------------------------------------------------
        # Suppress Short narration tracks when dedicated short generation is disabled
        # (default) or when this is an Episode Zero / validation run.
        _skip_shorts = (
            episode_id == "ep_000"
            or config.EPISODE_ZERO_MAX_SCENES > 0
            or not config.ENABLE_DEDICATED_SHORT_GENERATION
        )
        log.info("pipeline_stage_started", agent="audio_agent")
        _current_stage_id, _current_stage_t0 = _record_stage_start(
            run_id, episode_id, "audio_agent", log,
            output_schema="narration_package",
            db_path=db_path,
        )
        narration_package = _run_audio_agent(
            script_package=effective_script_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            skip_shorts=_skip_shorts,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="audio_agent")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="audio_agent")

        # -----------------------------------------------------------------------
        # 9. Assembly Agent
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="assembly_agent")
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
            script_package=effective_script_package,
            source_registry=research_package.get("source_registry", []),
            topic_brief=topic_brief,
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="assembly_agent")
        if cancel_check is not None and cancel_check():
            raise PipelineCancelledError(episode_id=episode_id, stage_after="assembly_agent")

        # -----------------------------------------------------------------------
        # 9b. Asset Archive — catalog all generated assets (non-blocking)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="asset_archive")
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

        # -----------------------------------------------------------------------
        # 11. Rights & Compliance Agent
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="compliance_agent")
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
        )
        _record_stage_complete(_current_stage_id, _current_stage_t0, log, db_path=db_path)
        _current_stage_id = None
        log.info("pipeline_stage_completed", agent="publishing_agent")

        # -----------------------------------------------------------------------
        # 13. Archive Agent — post-publish audit record (non-blocking)
        # -----------------------------------------------------------------------
        log.info("pipeline_stage_started", agent="archive_agent")
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
        # 16. Seal result
        # -----------------------------------------------------------------------
        complete_run(run_id=run_id, episode_id=episode_id)
        write_episode_rollup(run_id=run_id, episode_id=episode_id)
        clear_run_context()

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
            humanized_script_package=humanized_script_package,
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

    except PipelineCancelledError as _exc:
        # Operator cancelled the run between stages. Mark the run cancelled (not
        # failed) so the operator can distinguish intentional stops from errors.
        # Checkpoints on disk are preserved — the run can be resumed via rerun.
        if run_id is not None:
            try:
                cancel_run(run_id, episode_id)
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
        raise

    except Exception as _exc:
        if run_id is not None:
            try:
                fail_run(run_id, episode_id, error_message=str(_exc)[:4096])
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
        raise


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
    )


def _run_research_director(
    topic_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
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
    )


def _run_narrative_architect(
    research_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
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
    )


def _run_script_engine(
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
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
    )


_HUMANIZATION_MODE = "SHADOW"


def _run_humanization_stage(
    *,
    episode_id: str,
    script_package: dict,
    pipeline_run_id: str,
    output_dir: Optional[Path],
    config: Config,
    log,
) -> dict:
    """
    Humanization Stage (isolated capability — SHADOW mode only, hardcoded).

    Runs immediately after Script Engine. Delegates to
    hypogeum.agents.humanization_stage.run_humanization_stage(), which never
    mutates script_package — it only reads it and returns a separate
    humanized_script_package. No downstream stage consumes this package yet;
    it is produced, validated, checkpointed, and carried forward on
    PipelineResult only. SHADOW mode runs the pinned upstream Humanizer LLM
    pass for real (via hypogeum.agents.humanizer_llm_adapter) so its output
    can be reviewed and validated before any production dependency is ever
    created on it — Finance/VPD/Audio/Assembly all continue reading the
    original script_package, completely unaffected by this stage's result.

    Mode is hardcoded to "SHADOW" here — no config, env var, or feature flag
    governs it. Wiring a downstream consumer, or raising the mode further,
    are separate, future, explicitly authorized changes. Per-beat LLM
    failures never propagate out of run_humanization_stage() — they fall
    back to the deterministic-pass result inside the stage itself (see
    hypogeum/agents/humanization_stage.py::_build_shadow_beat()), so a model
    outage here can never halt the pipeline or affect script_package.

    Checkpoint: uses hypogeum.humanization_checkpoint's fingerprint-validated
    reuse check (stricter than _load_package()'s plain existence check —
    corruption raises rather than being treated as a cache miss). assets_root
    mirrors the same output_dir override every other stage's checkpoint
    honors for test isolation, falling back to the real .hypogeum-assets/
    root otherwise. model_id is included in the reuse check so a change to
    HUMANIZER_LLM_MODEL_ID correctly invalidates any prior SHADOW checkpoint.
    """
    assets_root = output_dir if output_dir is not None else _ASSETS_ROOT
    mode = _HUMANIZATION_MODE
    model_id = HUMANIZER_LLM_MODEL_ID if mode == "SHADOW" else None

    reused = load_reusable_humanization_checkpoint(
        assets_root=assets_root,
        episode_id=episode_id,
        script_package=script_package,
        mode=mode,
        model_id=model_id,
    )
    if reused is not None:
        log.info(
            "humanization_checkpoint_cache_hit",
            episode_id=episode_id,
            mode=mode,
            input_hash_prefix=reused["humanization"]["input_hash"][:12],
        )
        return reused

    log.info("humanization_checkpoint_cache_miss", episode_id=episode_id, mode=mode)

    t0 = datetime.now(timezone.utc)
    result = run_humanization_stage(
        episode_id=episode_id,
        script_package=script_package,
        pipeline_run_id=pipeline_run_id,
        mode=mode,
        config=config,
    )
    elapsed_ms = (datetime.now(timezone.utc) - t0).total_seconds() * 1000.0

    log.info(
        "humanization_validation_succeeded",
        episode_id=episode_id,
        mode=mode,
        elapsed_ms=round(elapsed_ms, 1),
    )

    save_humanization_checkpoint(assets_root=assets_root, episode_id=episode_id, package=result)

    return result


def _build_effective_script_package(
    script_package: dict,
    humanized_script_package: Optional[dict],
) -> dict:
    """
    Merge validated Humanizer narration into a schema-valid script_package
    shape — the package Finance authorization, Visual Package Director,
    Audio Agent, and Assembly Agent now receive instead of the raw,
    pre-Humanization script_package.

    Never mutates script_package — always returns a deep copy. Only
    sections[].narration_beats[].text and shorts_scripts[].narration_beats[].text
    are ever overwritten, and only for beats whose humanized_script_package
    beat record is accepted: transformation_status is anything other than
    "reverted" or "failed" (the only two statuses that mean the validated
    result was rejected and the original was already retained). Every other
    field — beat_id, section structure, timing data, visual directions,
    shorts structure, episode_metadata — is untouched by construction,
    since nothing but a matched beat's .text leaf is ever written.

    If every beat is reverted/failed (a full-stage fallback), or
    humanized_script_package is absent, the result is content-identical to
    script_package — the safe, original-narration default.

    Args:
        script_package:            The original, Script-Engine-produced
                                    package. Never mutated.
        humanized_script_package:  The Humanization Stage's output, or
                                    None (defensive — the pipeline always
                                    produces one today).

    Returns:
        A new dict, schema-shape-compatible with script_package.
    """
    effective = copy.deepcopy(script_package)

    if not humanized_script_package:
        return effective

    accepted_text_by_beat_id: dict = {}
    for beat in humanized_script_package.get("beats", []) or []:
        if not isinstance(beat, dict):
            continue
        beat_id = beat.get("beat_id")
        if not beat_id or beat.get("transformation_status") in ("reverted", "failed"):
            continue
        accepted_text_by_beat_id[beat_id] = beat.get("humanized_narration")

    def _apply_accepted_text(narration_beats) -> None:
        if not isinstance(narration_beats, list):
            return
        for nb in narration_beats:
            if not isinstance(nb, dict):
                continue
            accepted_text = accepted_text_by_beat_id.get(nb.get("beat_id"))
            if isinstance(accepted_text, str) and accepted_text.strip():
                nb["text"] = accepted_text

    for section in effective.get("sections", []) or []:
        if isinstance(section, dict):
            _apply_accepted_text(section.get("narration_beats", []))
    for short in effective.get("shorts_scripts", []) or []:
        if isinstance(short, dict):
            _apply_accepted_text(short.get("narration_beats", []))

    return effective


def _run_visual_package_director(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
) -> dict:
    """
    Visual Package Director (M2.3/M2.4 — implemented).

    Phase 1: Claude Sonnet 4.6 plans scenes, entity lock states, and thumbnail
    candidates. Phase 2: MUAPI generates reference images (final-locked entities)
    then all scene videos. Also extracts and writes Thumbnail Handoff Brief (M2.4).

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
        phase2_done = bool(assets) and all(
            a.get("generation_status") in _TERMINAL_SUCCESS for a in assets
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
            if a.get("generation_status") not in _TERMINAL_SUCCESS
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
        )
    return run_visual_package_director(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )


def _run_thumbnail_director(
    visual_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
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

    Checkpoint: loads existing thumbnail_package from disk if present.
    """
    pkg = _load_package(episode_id, "thumbnail_package", output_dir)
    if pkg is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
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
    )


def _run_audio_agent(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    skip_shorts: bool = False,
) -> dict:
    """
    Audio Agent (M3.2–M3.6 — implemented).

    Delegates to hypogeum.agents.audio_agent.run_audio_agent().
    Orchestrates ElevenLabs beat-level narration generation, MP3 stitching,
    SRT caption generation (with Whisper fallback), and seals the Narration Package.

    No Claude calls — pure Python orchestration over elevenlabs.py.

    Max tokens: 2,048 (not applicable — no LLM call)
    Output schema: narration_package
    """
    return run_audio_agent(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        skip_shorts=skip_shorts,
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
    """
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


def _run_publishing_agent(
    final_render_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    youtube_privacy: str = "public",
    publish_at: Optional[str] = None,
) -> dict:
    """
    Publishing Agent (M5.2 — implemented).

    Delegates to hypogeum.agents.publishing_agent.run_publishing_agent().
    Distributes the main episode (16:9) and all 3 Shorts (9:16) to YouTube,
    Instagram Reels, Facebook Video, and TikTok (if available). Applies
    platform-specific metadata, AI disclosure text, and retry policy.

    No Claude calls — pure Python orchestration over platform client modules.

    Max tokens: 2,048 (not applicable — no LLM call)
    Output schema: publishing_record
    """
    return run_publishing_agent(
        final_render_package=final_render_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        youtube_privacy=youtube_privacy,
        publish_at=publish_at,
    )
