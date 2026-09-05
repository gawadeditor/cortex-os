"""
hypogeum/agents/brand_guardian_agent.py — Brand Guardian Agent v1A

Pre-generation brand compliance gate. Enforces The Hypogeum Chronicles visual
identity by scanning scene descriptions and thumbnail plans BEFORE any paid
generation (MUAPI/image) takes place.

Two passes:
  Pass 1 — Visual Plan validation (after VPD Phase 1, before MUAPI scene generation)
  Pass 2 — Thumbnail Plan validation (after TD Phase 1, before thumbnail image generation)

Public API:
    run_bga_pass1(visual_package, episode_id, config, output_dir=None) → dict
    run_bga_pass2(thumbnail_generation_plan, episode_id, config, output_dir=None) → dict

Raises BGARejectedError when the compliance report outcome is REJECTED.

Rejection thresholds:
  Pass 1: REJECTED if any BLOCKING violation, or ≥25% of scenes have FLAG violations.
  Pass 2: REJECTED if rank-1 main candidate has BLOCKING, or all main candidates BLOCKING.

Output: bga_compliance_report_{pass_number}_{episode_id}.json written to episode dir.

Enforcement scope (v1A — ENFORCEABLE_CORE only):
  BLOCKING: talking heads, lip sync, direct-to-camera, interview framing (E-VG05/E-VG06)
  BLOCKING: neon, cyberpunk, modern urban, bright-key, cool blue, green, pink/purple (E-C02–E-C08)
  BLOCKING: thumbnail human face, light background (E-TH01, E-TH07)
  BLOCKING: wordmark structure violations when wordmark is specified (E-L01–E-L09)
  BLOCKING: non-Cinzel/Raleway font when explicitly named in prompt (E-T01–E-T03)
  FLAG: off-register mood, bright/commercial atmosphere, explicitly saturated palette

NOT enforced (v1A):
  Exact hex values (all NOT_VERIFIED in bible v1.1)
  Music identity (absent from visual brand spec)
  Pixel-level generated output analysis (Pass 3 — deferred)
  AI disclosure wording, YouTube title limits, watermark, end-card policy

Integration contracts:
  - Claude calls go through call_claude() — never requests directly
  - System prompt loaded from disk — never hardcoded inline
  - Logging via get_logger() — never print() or stdlib logging
  - No environment access — all settings via Config
  - Violation parsing and rejection threshold applied in Python (not by Claude)

Model: config.BGA_PASS1_MODEL (Pass 1, default: anthropic/claude-sonnet-4.6)
       config.BGA_PASS2_MODEL (Pass 2, default: openai/gpt-5.5-pro)
Max tokens: 8,192 (large visual packages have many scenes)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.failure_policy import FailurePolicy
from hypogeum.logger import get_logger
from hypogeum.openrouter import call_claude
from hypogeum.prompt_loader import load_prompt
from hypogeum.schemas import SchemaError, validate

# Lazy import — avoids circular dependency at module load time
def _get_run_context() -> dict:
    from hypogeum.finance import get_run_context
    return get_run_context()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "brand_guardian_agent"
_BGA_MODEL = "anthropic/claude-sonnet-4.6"
_BGA_MAX_TOKENS = 8_192
_BIBLE_VERSION = "1.1"
_BGA_VERSION = "1A"

# Pass 1: reject if flag_count / total_scenes >= this threshold
_PASS1_FLAG_REJECTION_THRESHOLD = 0.25

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "brand_guardian_agent.txt"

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BGARejectedError(RuntimeError):
    """
    Raised by run_bga_pass1() or run_bga_pass2() when the compliance report
    outcome is REJECTED.

    failure_policy = TERMINAL: a BGA rejection is a verdict on this exact
    visual_package's scene descriptions — an unchanged re-run produces the
    identical verdict, so it must NOT be auto-retried (that was an infinite
    loop, observed 2026-09-06 on ep_8a11b078). The topic is skipped and an
    operator (or a future scene-description-rewrite pass) resolves it,
    exactly as a QA rejection is handled.

    Attributes:
        episode_id:  Episode identifier.
        pass_number: 1 or 2.
        report:      The full bga_compliance_report dict attached for inspection.
    """

    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, pass_number: int, report: dict) -> None:
        self.episode_id = episode_id
        self.pass_number = pass_number
        self.report = report
        blocking = report.get("summary", {}).get("blocking_count", 0)
        reason = report.get("summary", {}).get(
            "rejection_reason", "brand compliance failure"
        )
        super().__init__(
            f"BGA Pass {pass_number} REJECTED for {episode_id!r}: "
            f"{blocking} blocking violation(s). Reason: {reason}"
        )


# ---------------------------------------------------------------------------
# Internal helpers — parsing
# ---------------------------------------------------------------------------

def _strip_fence(content: str) -> str:
    stripped = content.strip()
    m = _FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_violations(raw: str) -> list[dict]:
    """
    Parse Claude's raw response content into a list of violation dicts.

    Returns an empty list if the response cannot be parsed or is malformed.
    Never raises.
    """
    content = _strip_fence(raw)
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            v = obj.get("violations")
            if isinstance(v, list):
                return v
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _normalize_violation(v: object) -> Optional[dict]:
    """
    Normalize a single violation entry. Returns None for entries that are
    missing required fields or have invalid severity values.
    """
    if not isinstance(v, dict):
        return None
    item_id = str(v.get("item_id", "")).strip()
    rule_id = str(v.get("rule_id", "")).strip()
    severity = v.get("severity", "")
    description = str(v.get("description", "")).strip()
    if not item_id or not rule_id or severity not in ("BLOCKING", "FLAG") or not description:
        return None
    return {
        "item_id": item_id,
        "rule_id": rule_id,
        "severity": severity,
        "description": description,
        "prompt_excerpt": str(v.get("prompt_excerpt", ""))[:200],
    }


def _clean_violations(raw_list: list) -> list[dict]:
    return [n for v in raw_list if (n := _normalize_violation(v)) is not None]


# ---------------------------------------------------------------------------
# Internal helpers — outcome computation
# ---------------------------------------------------------------------------

def _compute_outcome_pass1(
    violations: list[dict],
    total_scenes: int,
) -> tuple[str, Optional[str]]:
    """
    Apply Pass 1 rejection thresholds.

    Returns (outcome, rejection_reason). rejection_reason is None when outcome
    is APPROVED or APPROVED_WITH_FLAGS.
    """
    blocking = [v for v in violations if v["severity"] == "BLOCKING"]
    flags = [v for v in violations if v["severity"] == "FLAG"]

    if blocking:
        return (
            "REJECTED",
            f"{len(blocking)} blocking violation(s) in scene descriptions",
        )

    if total_scenes > 0:
        flag_rate = len(flags) / total_scenes
        if flag_rate >= _PASS1_FLAG_REJECTION_THRESHOLD:
            return (
                "REJECTED",
                (
                    f"{len(flags)} flag violation(s) across {total_scenes} scene(s) "
                    f"({flag_rate:.0%}) exceeds {_PASS1_FLAG_REJECTION_THRESHOLD:.0%} threshold"
                ),
            )

    if flags:
        return "APPROVED_WITH_FLAGS", None

    return "APPROVED", None


def _compute_outcome_pass2(
    violations: list[dict],
    generation_plan: dict,
) -> tuple[str, Optional[str]]:
    """
    Apply Pass 2 rejection thresholds.

    REJECTED if:
      - The rank-1 main candidate has any BLOCKING violation, OR
      - All main candidates have BLOCKING violations (no viable candidate).

    Returns (outcome, rejection_reason).
    """
    blocking = [v for v in violations if v["severity"] == "BLOCKING"]
    flags = [v for v in violations if v["severity"] == "FLAG"]

    main_cands = generation_plan.get("main_episode", {}).get("ranked_candidates", [])
    all_main_ids = {c.get("candidate_id") for c in main_cands if c.get("candidate_id")}
    rank1_ids = {c.get("candidate_id") for c in main_cands if c.get("rank") == 1}

    blocking_ids = {v["item_id"] for v in blocking}

    rank1_blocking = rank1_ids & blocking_ids
    if rank1_blocking:
        return "REJECTED", "Rank-1 main thumbnail candidate has blocking violation(s)"

    if all_main_ids and all_main_ids <= blocking_ids:
        return "REJECTED", "All main thumbnail candidates have blocking violation(s)"

    if blocking or flags:
        return "APPROVED_WITH_FLAGS", None

    return "APPROVED", None


# ---------------------------------------------------------------------------
# Internal helpers — report building and writing
# ---------------------------------------------------------------------------

def _build_report(
    pass_number: int,
    episode_id: str,
    violations: list[dict],
    outcome: str,
    total_items: int,
    rejection_reason: Optional[str],
) -> dict:
    blocking_count = sum(1 for v in violations if v["severity"] == "BLOCKING")
    flag_count = sum(1 for v in violations if v["severity"] == "FLAG")

    summary: dict = {
        "total_items": total_items,
        "blocking_count": blocking_count,
        "flag_count": flag_count,
    }
    if rejection_reason:
        summary["rejection_reason"] = rejection_reason

    return {
        "report_id": f"bga_p{pass_number}_{episode_id}",
        "pass_number": pass_number,
        "episode_id": episode_id,
        "bible_version": _BIBLE_VERSION,
        "bga_version": _BGA_VERSION,
        "produced_at": _now_iso(),
        "outcome": outcome,
        "violations": violations,
        "summary": summary,
    }


def _write_report(
    report: dict,
    episode_id: str,
    pass_number: int,
    output_dir: Path,
) -> None:
    out_path = output_dir / f"bga_compliance_report_p{pass_number}_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_gate_verdict(
    report: dict,
    pass_number: int,
    model_slug: str,
) -> None:
    """
    Persist a BGA verdict row to gate_verdicts. Called for ALL outcomes —
    APPROVED, APPROVED_WITH_FLAGS, and REJECTED — so the audit trail is complete.
    Reads run_id and db_path from the active run context (set by pipeline).

    Non-blocking: any exception is caught and logged. A failed DB write
    must never interrupt the BGA flow or cause a run to fail.
    """
    ctx = _get_run_context()
    run_id  = ctx.get("run_id")
    db_path = ctx.get("db_path")

    if not run_id:
        return  # no active run context — standalone BGA call, skip write

    try:
        import uuid as _uuid
        from hypogeum.db import get_connection, initialize_schema
        from pathlib import Path as _Path

        initialize_schema(db_path)
        db = _Path(db_path) if db_path else None

        summary    = report.get("summary", {})
        violations = report.get("violations", [])
        rule_ids   = ",".join(sorted({v["rule_id"] for v in violations if v.get("rule_id")}))

        conn = get_connection(db)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO gate_verdicts
                   (verdict_id, run_id, episode_id, stage_exec_id,
                    gate_name, verdict, evaluated_at,
                    asset_id, rule_ids_flagged, flag_count,
                    blocking_count, warning_count, model_slug,
                    full_report, metadata_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(_uuid.uuid4()),
                    str(run_id),
                    str(report.get("episode_id") or ""),
                    None,                          # stage_exec_id — not tracked at BGA level
                    f"bga_pass{pass_number}",
                    str(report.get("outcome") or ""),
                    str(report.get("produced_at") or _now_iso()),
                    None,                          # asset_id — not applicable for scene checks
                    rule_ids or None,
                    int(summary.get("flag_count", 0)),
                    int(summary.get("blocking_count", 0)),
                    0,                             # warning_count — BGA uses BLOCKING/FLAG only
                    str(model_slug)[:200] if model_slug else None,
                    json.dumps(report, ensure_ascii=False),
                    "v1",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass   # observability write — never propagates


def _call_bga(
    input_package: dict,
    config: Config,
    episode_id: str,
    model_slug: str = _BGA_MODEL,
) -> str:
    """Call Claude for BGA evaluation. Returns raw response content string."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": json.dumps(input_package, ensure_ascii=False)},
    ]
    result = call_claude(
        model_slug=model_slug,
        messages=messages,
        max_tokens=_BGA_MAX_TOKENS,
        config=config,
        episode_id=episode_id,
        agent=_AGENT_NAME,
    )
    return result.content


# ---------------------------------------------------------------------------
# Pass 1 — Visual Plan Validation
# ---------------------------------------------------------------------------

def _extract_pass1_scenes(visual_package: dict) -> list[dict]:
    """Extract compact scene data relevant for brand compliance checking."""
    compact = []
    for scene in visual_package.get("scenes", []):
        spec = scene.get("production_spec", {})
        vs = scene.get("visual_spec", {})
        compact.append({
            "scene_id": scene.get("scene_id", ""),
            "primary_prompt": spec.get("primary_prompt", ""),
            "fallback_prompt": spec.get("fallback_prompt", ""),
            "subject": vs.get("subject", ""),
            "lighting": vs.get("lighting", ""),
            "mood": vs.get("mood", ""),
            "atmosphere": vs.get("atmosphere", ""),
            "color_treatment": vs.get("color_treatment", ""),
        })
    return compact


def run_bga_pass1(
    visual_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Brand Guardian Pass 1: validate visual scene descriptions before MUAPI generation.

    Scans every scene's production_spec prompts and visual_spec fields for brand
    violations. Any BLOCKING violation, or a FLAG rate ≥25% of total scenes,
    causes a REJECTED outcome and raises BGARejectedError.

    Args:
        visual_package: Validated visual_package dict from VPD Phase 1.
        episode_id:     Episode identifier for log correlation and file naming.
        config:         Loaded Config instance.
        output_dir:     Optional output directory override.

    Returns:
        The bga_compliance_report dict (always, even for APPROVED_WITH_FLAGS).

    Raises:
        BGARejectedError — outcome is REJECTED; report is attached to the exception.
    """
    _resolve_output_dir = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    compact_scenes = _extract_pass1_scenes(visual_package)
    total_scenes = len(compact_scenes)

    log.info(
        "bga_pass1_started",
        episode_id=episode_id,
        total_scenes=total_scenes,
    )

    input_package = {
        "pass_number": 1,
        "episode_id": episode_id,
        "total_scenes": total_scenes,
        "scenes": compact_scenes,
    }

    raw_content = _call_bga(
        input_package, config, episode_id,
        model_slug=getattr(config, "BGA_PASS1_MODEL", _BGA_MODEL),
    )
    raw_violations = _parse_violations(raw_content)
    violations = _clean_violations(raw_violations)

    outcome, rejection_reason = _compute_outcome_pass1(violations, total_scenes)
    report = _build_report(1, episode_id, violations, outcome, total_scenes, rejection_reason)

    try:
        validate("bga_compliance_report", report)
    except SchemaError as exc:
        log.warn("bga_schema_warn", pass_number=1, path=exc.path, reason=exc.reason)

    _write_report(report, episode_id, 1, _resolve_output_dir)

    _pass1_model = getattr(config, "BGA_PASS1_MODEL", _BGA_MODEL)
    _write_gate_verdict(report, 1, _pass1_model)

    log.info(
        "bga_pass1_completed",
        outcome=outcome,
        total_scenes=total_scenes,
        blocking_count=report["summary"]["blocking_count"],
        flag_count=report["summary"]["flag_count"],
    )

    if outcome == "REJECTED":
        log.error(
            "bga_pass1_rejected",
            episode_id=episode_id,
            blocking_count=report["summary"]["blocking_count"],
            rejection_reason=rejection_reason or "brand compliance failure",
        )
        raise BGARejectedError(episode_id, 1, report)

    return report


# ---------------------------------------------------------------------------
# Pass 2 — Thumbnail Plan Validation
# ---------------------------------------------------------------------------

def _extract_pass2_candidates(generation_plan: dict) -> list[dict]:
    """Extract compact thumbnail candidate data for Pass 2 evaluation."""
    candidates = []

    main_cands = generation_plan.get("main_episode", {}).get("ranked_candidates", [])
    for c in main_cands:
        overlay = c.get("text_overlay")
        candidates.append({
            "candidate_id": c.get("candidate_id", ""),
            "source": "main_episode",
            "rank": c.get("rank", 0),
            "image_prompt": c.get("image_prompt", ""),
            "text_overlay": overlay.get("text", "") if isinstance(overlay, dict) else "",
        })

    for short in generation_plan.get("shorts", []):
        short_id = short.get("short_id", "")
        for c in short.get("ranked_candidates", []):
            overlay = c.get("text_overlay")
            candidates.append({
                "candidate_id": c.get("candidate_id", ""),
                "source": f"short:{short_id}",
                "rank": c.get("rank", 0),
                "image_prompt": c.get("image_prompt", ""),
                "text_overlay": overlay.get("text", "") if isinstance(overlay, dict) else "",
            })

    return candidates


def run_bga_pass2(
    thumbnail_generation_plan: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Brand Guardian Pass 2: validate thumbnail candidates before image generation.

    Scans every candidate's image_prompt and text_overlay for brand violations.
    REJECTED if rank-1 main candidate has any BLOCKING violation, or if all main
    candidates are BLOCKING (no viable thumbnail exists).

    Args:
        thumbnail_generation_plan: Validated thumbnail_generation_plan dict from TD Phase 1.
        episode_id:                Episode identifier.
        config:                    Loaded Config instance.
        output_dir:                Optional output directory override.

    Returns:
        The bga_compliance_report dict.

    Raises:
        BGARejectedError — outcome is REJECTED; report is attached to the exception.
    """
    _resolve_output_dir = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    compact_candidates = _extract_pass2_candidates(thumbnail_generation_plan)
    total_candidates = len(compact_candidates)

    log.info(
        "bga_pass2_started",
        episode_id=episode_id,
        total_candidates=total_candidates,
    )

    input_package = {
        "pass_number": 2,
        "episode_id": episode_id,
        "total_candidates": total_candidates,
        "candidates": compact_candidates,
    }

    raw_content = _call_bga(
        input_package, config, episode_id,
        model_slug=getattr(config, "BGA_PASS2_MODEL", _BGA_MODEL),
    )
    raw_violations = _parse_violations(raw_content)
    violations = _clean_violations(raw_violations)

    outcome, rejection_reason = _compute_outcome_pass2(violations, thumbnail_generation_plan)
    report = _build_report(2, episode_id, violations, outcome, total_candidates, rejection_reason)

    try:
        validate("bga_compliance_report", report)
    except SchemaError as exc:
        log.warn("bga_schema_warn", pass_number=2, path=exc.path, reason=exc.reason)

    _write_report(report, episode_id, 2, _resolve_output_dir)

    _pass2_model = getattr(config, "BGA_PASS2_MODEL", _BGA_MODEL)
    _write_gate_verdict(report, 2, _pass2_model)

    log.info(
        "bga_pass2_completed",
        outcome=outcome,
        total_candidates=total_candidates,
        blocking_count=report["summary"]["blocking_count"],
        flag_count=report["summary"]["flag_count"],
    )

    if outcome == "REJECTED":
        log.error(
            "bga_pass2_rejected",
            episode_id=episode_id,
            blocking_count=report["summary"]["blocking_count"],
            rejection_reason=rejection_reason or "brand compliance failure",
        )
        raise BGARejectedError(episode_id, 2, report)

    return report
