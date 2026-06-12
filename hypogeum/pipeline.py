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

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hypogeum.agents.asset_archive_agent import run_asset_archive
from hypogeum.agents.assembly_agent import run_assembly_agent
from hypogeum.agents.audio_agent import run_audio_agent
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
from hypogeum.agents.visual_package_director import (
    run_visual_package_director,
    run_visual_package_director_from_plan,
)
from hypogeum.config import Config
from hypogeum.finance import clear_run_context, set_run_context, write_episode_rollup
from hypogeum.production_metadata import write_analytics_pending, write_production_metadata
from hypogeum.logger import get_logger
from hypogeum.run_registry import complete_run, fail_run, register_run
from hypogeum.startup import StartupReport, run_startup_checks


# ---------------------------------------------------------------------------
# Model slug constants (frozen — T0.1 model assignments)
# ---------------------------------------------------------------------------

_SONNET = "anthropic/claude-sonnet-4.6"
_OPUS   = "anthropic/claude-opus-4.6"
_FABLE5 = "anthropic/claude-fable-5"


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    topic_input: str,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
    youtube_privacy: str = "public",
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

    Returns:
        PipelineResult — all 11 packages populated on success.

    Raises:
        StartupError           — no viable API path (OpenRouter unavailable AND
                                 no ANTHROPIC_API_KEY). Pipeline never starts.
        PipelineRejectedError  — EP scored topic below REJECT threshold (< 6.0).
        PackageDeficiencyError — RD issued a PDR. Operator must resolve and re-run.
        NotImplementedError    — agent stub not yet implemented (M1–M5 deliverables).
        AgentError             — run_agent() failed (parse, schema, or write error).
    """
    log = get_logger(episode_id=episode_id, agent="pipeline")

    log.info(
        "pipeline_run_started",
        episode_id=episode_id,
        topic_input=topic_input,
    )

    # -----------------------------------------------------------------------
    # 0. Run registration — must happen before any agent or spend
    # -----------------------------------------------------------------------
    run_id = register_run(episode_id=episode_id, config=config)
    set_run_context(run_id=run_id, episode_id=episode_id)
    log.info("pipeline_run_registered", run_id=run_id)

    # -----------------------------------------------------------------------
    # 1. Startup verification — fail-fast before any agent is called
    # -----------------------------------------------------------------------
    startup_report = run_startup_checks(config, episode_id=episode_id)

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
    validation_scorecard, topic_brief = _run_executive_producer(
        topic_input=topic_input,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )

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

    # -----------------------------------------------------------------------
    # 3. Research Director
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="research_director")
    research_package, pdr = _run_research_director(
        topic_brief=topic_brief,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )

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

    # -----------------------------------------------------------------------
    # 4. Narrative Architect
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="narrative_architect")
    narrative_package = _run_narrative_architect(
        research_package=research_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
    log.info("pipeline_stage_completed", agent="narrative_architect")

    # -----------------------------------------------------------------------
    # 5. Script Engine
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="script_engine")
    script_package = _run_script_engine(
        narrative_package=narrative_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
    log.info("pipeline_stage_completed", agent="script_engine")

    # -----------------------------------------------------------------------
    # 6. Finance Agent — budget authorization gate (pre-generation spend)
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="finance_agent_authorization")
    finance_authorization = _run_budget_authorization(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
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

    # -----------------------------------------------------------------------
    # 7. Visual Package Director
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="visual_package_director")
    visual_package = _run_visual_package_director(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
    log.info("pipeline_stage_completed", agent="visual_package_director")

    # -----------------------------------------------------------------------
    # 7. Thumbnail Director
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="thumbnail_director")
    thumbnail_package = _run_thumbnail_director(
        visual_package=visual_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
    log.info("pipeline_stage_completed", agent="thumbnail_director")

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
    narration_package = _run_audio_agent(
        script_package=script_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        skip_shorts=_skip_shorts,
    )
    log.info("pipeline_stage_completed", agent="audio_agent")

    # -----------------------------------------------------------------------
    # 9. Assembly Agent
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="assembly_agent")
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
    )
    log.info("pipeline_stage_completed", agent="assembly_agent")

    # -----------------------------------------------------------------------
    # 9b. Asset Archive — catalog all generated assets (non-blocking)
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="asset_archive")
    asset_archive_record = _run_asset_archive(
        visual_package=visual_package,
        narration_package=narration_package,
        thumbnail_package=thumbnail_package,
        final_render_package=final_render_package,
        episode_id=episode_id,
        output_dir=output_dir,
        run_id=run_id,
    )
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
    qa_approval_report = _run_qa_agent(
        final_render_package=final_render_package,
        thumbnail_package=thumbnail_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
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

    # -----------------------------------------------------------------------
    # 11. Rights & Compliance Agent
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="compliance_agent")
    compliance_report = _run_compliance_agent(
        final_render_package=final_render_package,
        source_registry=research_package.get("source_registry", []),
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
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

    # -----------------------------------------------------------------------
    # 12. Publishing Agent
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="publishing_agent")
    publishing_record = _run_publishing_agent(
        final_render_package=final_render_package,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        youtube_privacy=youtube_privacy,
    )
    log.info("pipeline_stage_completed", agent="publishing_agent")

    # -----------------------------------------------------------------------
    # 13. Archive Agent — post-publish audit record (non-blocking)
    # -----------------------------------------------------------------------
    log.info("pipeline_stage_started", agent="archive_agent")
    archive_record = _run_archive_agent(
        final_render_package=final_render_package,
        publishing_record=publishing_record,
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
    )
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
    finance_record = _run_cost_ledger(
        final_render_package=final_render_package,
        publishing_record=publishing_record,
        archive_record=archive_record,
        config=config,
        episode_id=episode_id,
        narration_package=narration_package,
        visual_package=visual_package,
        output_dir=output_dir,
    )
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


# ---------------------------------------------------------------------------
# Agent stubs
#
# Each stub is the integration point for one agent. Stubs raise
# NotImplementedError until implemented in M1–M5. The pipeline calls them via
# the names below so tests can mock individual agents independently.
#
# Model assignments (current — see .env for overrides):
#   EP → claude-fable-5 (config.EP_MODEL)
#   RD → claude-opus-4.6 (config.RD_MODEL)
#   TD planning → gpt-5.5-pro (config.TD_PLANNING_MODEL)
#   BGA Pass 1 → claude-sonnet-4.6 (config.BGA_PASS1_MODEL)
#   BGA Pass 2 → gpt-5.5-pro (config.BGA_PASS2_MODEL)
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

    Checkpoint: if a visual_package plan already exists on disk (Phase 1 done,
    Phase 2 not yet run), skips Phase 1 and resumes directly at Phase 2.
    """
    existing_plan = _load_package(episode_id, "visual_package", output_dir)
    if existing_plan is not None:
        log = get_logger(episode_id=episode_id, agent="pipeline")
        log.info(
            "pipeline_stage_skipped",
            agent="visual_package_director_phase1",
            reason="existing_plan",
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
    """
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


def _run_publishing_agent(
    final_render_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path],
    youtube_privacy: str = "public",
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
    )
