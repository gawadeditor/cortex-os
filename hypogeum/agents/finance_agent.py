"""
hypogeum/agents/finance_agent.py — Finance Agent (R50)

Two-phase budget gate and cost ledger for the hypogeum pipeline.

Phase 1 — Budget Authorization (pre-run gate):
    Called after Script Engine, before Visual Package Director.
    Estimates planned spend from script_package.
    Blocks generation when estimated spend exceeds configured ceiling.
    Verdicts: AUTHORIZED / WARNING / BLOCKED

Phase 2 — Cost Ledger (post-run record):
    Called after Archive Agent. Non-blocking.
    Records actual cost fields from available pipeline artifacts.
    Appends a line to the append-only JSONL ledger if configured.
    Verdict: RECORDED

Both phases emit a finance_record_{episode_id}.json (authorization) or
finance_record_{episode_id}.json (ledger overwrites after the run).
Authorization output is named finance_authorization_{episode_id}.json to
avoid collision; the ledger output is finance_record_{episode_id}.json.

Responsibilities:
    - Read configured budget ceiling (FINANCE_BUDGET_CEILING_USD)
    - Estimate planned spend from script_package inputs (narration + video + LLM + music)
    - Gate pipeline when estimate exceeds ceiling (BLOCKED verdict)
    - Record actual provider spend from pipeline artifacts post-run
    - Maintain append-only cost ledger if FINANCE_LEDGER_PATH is configured

Prohibited responsibilities (owned elsewhere):
    - Rendering or publishing decisions (Assembly Agent / Publishing Agent)
    - Provider API calls (MUAPI client, ElevenLabs client)
    - Content quality evaluation (QA Agent)
    - Rights / compliance checks (Compliance Agent)
    - Archiving episode artifacts (Archive Agent)

No Claude calls — pure deterministic evaluation.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.policy_cascade import get_effective_budget_ceiling
from hypogeum.schemas import validate

# ---------------------------------------------------------------------------
# Cost constants (from benchmarks — memory: Seedance benchmark 2026-06-06)
# ---------------------------------------------------------------------------

_ELEVENLABS_COST_PER_1K_CHARS: float = 0.30   # USD per 1,000 characters billed
_MUAPI_COST_PER_CLIP: float = 0.15            # USD per clip (Runway rate, conservative)
_MUAPI_CLIPS_PER_SECTION: int = 3             # clips planned per main-episode section
_OPENROUTER_PLANNING_FLAT: float = 0.05       # flat estimate for all LLM planning calls
_MUSIC_COST_PER_SECTION: float = 0.20         # USD per section (ElevenLabs Music generation)

# Image model costs (USD per image generation call)
_MUAPI_IMAGE_COSTS: dict = {
    "flux-dev-image":     0.01,
    "flux-schnell-image": 0.005,
}
_MUAPI_IMAGE_DEFAULT_COST: float = 0.01   # fallback for unknown image models

# Music source that incurs no provider cost
_MUSIC_FREE_SOURCE: str = "channel_library"
_ELEVENLABS_MUSIC_COST_PER_TRACK: float = 0.20   # USD per generated music track

# Conservative fallback for model cost when model slug is not in MODEL_CREDIT_COSTS
_MUAPI_UNKNOWN_MODEL_COST: float = 2.5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_narration_chars(script_package: dict) -> int:
    """Sum character counts of all narration beat texts in the script."""
    total = 0
    for section in script_package.get("sections", []):
        for beat in section.get("narration_beats", []):
            if isinstance(beat, dict):
                total += len(beat.get("text", ""))
            elif isinstance(beat, str):
                total += len(beat)
    # Include shorts narration beats
    for short in script_package.get("shorts_scripts", []):
        for beat in short.get("narration_beats", []):
            if isinstance(beat, dict):
                total += len(beat.get("text", ""))
            elif isinstance(beat, str):
                total += len(beat)
    return total


def _estimate_section_count(script_package: dict) -> int:
    return len(script_package.get("sections", []))


def _read_narration_chars_billed(narration_package: Optional[dict]) -> int:
    """
    Read actual billed character count from narration_package.audio_qa_report.

    Returns 0 when narration_package is absent or the field is missing.
    """
    if not narration_package:
        return 0
    return int(narration_package.get("audio_qa_report", {}).get("total_characters_billed", 0))


def _count_generated_clips(visual_package: Optional[dict]) -> int:
    """
    Count MUAPI video generation API calls from visual_package asset manifest.

    Counts only assets where generation_status == "generated".
    Assets with generation_status == "reused" are excluded — no API call was made.
    Returns 0 when visual_package is absent or the manifest is empty.
    """
    if not visual_package:
        return 0
    assets = visual_package.get("asset_manifest", {}).get("assets", [])
    return sum(1 for a in assets if a.get("generation_status") == "generated")


def _compute_thumbnail_cost(final_render_package: dict) -> float:
    """
    Estimate thumbnail generation cost from final_render_package.thumbnail_references.

    Each non-empty thumbnail reference represents one image generation call.
    Uses _MUAPI_IMAGE_DEFAULT_COST as the per-image rate when model is unknown.
    Returns 0.0 when no thumbnail references are present.
    """
    refs = final_render_package.get("thumbnail_references", [])
    count = sum(1 for r in refs if r.get("thumbnail_asset_id"))
    return count * _MUAPI_IMAGE_DEFAULT_COST


def _compute_music_cost(final_render_package: dict) -> float:
    """
    Compute music provider cost from final_render_package.music_log.

    channel_library tracks are free (no provider API call).
    All other sources incur _ELEVENLABS_MUSIC_COST_PER_TRACK per track.
    Returns 0.0 when music_log is absent or empty.
    """
    music_log = final_render_package.get("music_log", [])
    cost = 0.0
    for entry in music_log:
        if entry.get("source") != _MUSIC_FREE_SOURCE:
            cost += _ELEVENLABS_MUSIC_COST_PER_TRACK
    return cost


def _add_check(
    checks: list,
    failures: list,
    warnings: list,
    check_id: str,
    status: str,
    detail: str,
) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
    if status == "fail":
        failures.append(check_id)
    elif status == "warn":
        warnings.append(check_id)


class FinancePostVPDBlockedError(Exception):
    """
    Raised by run_post_vpd_authorization() callers when the post-VPD budget
    gate returns verdict="BLOCKED". This halts the pipeline before any MUAPI
    video generation spend is incurred.
    """
    def __init__(self, episode_id: str, record: dict):
        self.episode_id = episode_id
        self.record = record
        super().__init__(
            f"Post-VPD finance gate BLOCKED for {episode_id}: "
            f"estimated ${record.get('estimated_spend_usd', '?')} USD "
            f"exceeds ${record.get('budget_ceiling_usd', '?')} USD ceiling"
        )


def _resolve_scene_model_estimate(
    scene: dict,
    final_entity_ids: set,
    config,
) -> str:
    """
    Predict the MUAPI model for a scene from its Phase 1 VPD plan.

    Mirrors submit_scene() / _tier_to_model() logic but predicts
    reference_image_url presence from entity lock state — reference images
    are not yet generated at Phase 1 planning time.

    A scene will use the I2V model when its primary entity is a final-locked
    entity (because a reference image will be generated for it in Phase 2).

    Args:
        scene:            Scene dict from visual_package.scenes[].
        final_entity_ids: Set of entity_id strings with lock_state == "final".
        config:           Loaded Config instance.

    Returns:
        MUAPI model slug string.
    """
    # Predict I2V: scene has a primary entity that is final-locked.
    # cheap_mode disables reference image conditioning entirely.
    will_use_i2v = (
        not config.MUAPI_CHEAP_MODE
        and any(
            a.get("entity_id") in final_entity_ids
            for a in scene.get("entity_appearances", [])
            if a.get("role") == "primary"
        )
    )

    if will_use_i2v:
        return config.MUAPI_I2V_MODEL

    # Apply mode overrides (same precedence as submit_scene())
    if config.MUAPI_CHEAP_MODE:
        tier = 3
    elif config.MUAPI_RUNWAY_ONLY_MODE:
        tier = 2
    else:
        prod = scene.get("production_spec", {})
        routing = prod.get("provider_routing", {})
        raw_tier = routing.get("primary_tier")
        tier = int(raw_tier) if raw_tier is not None else config.MUAPI_DEFAULT_TIER

    if tier == 1:
        return config.MUAPI_T1_MODEL
    if tier == 3:
        return config.MUAPI_T3_MODEL
    return config.MUAPI_T2_MODEL


def _estimate_muapi_from_vpd_plan(
    visual_package: dict,
    config,
) -> tuple:
    """
    Estimate total MUAPI video generation cost from the Phase 1 VPD plan.

    Iterates all planned scenes, predicts each scene's model from tier routing
    and entity lock state, then sums costs from MODEL_CREDIT_COSTS.

    Args:
        visual_package: Phase 1 VPD output (generation_status = "planned").
        config:         Loaded Config instance.

    Returns:
        (total_cost_usd: float, model_breakdown: dict[str, int])
        model_breakdown maps model slug → scene count.
    """
    from hypogeum.muapi import MODEL_CREDIT_COSTS

    # Build set of final-locked entity IDs for I2V prediction
    locked_entities = (
        visual_package
        .get("visual_continuity_record", {})
        .get("locked_entities", [])
    )
    final_entity_ids = {
        e["entity_id"]
        for e in locked_entities
        if e.get("lock_state") == "final" and "entity_id" in e
    }

    scenes = visual_package.get("scenes", [])
    total_cost = 0.0
    model_breakdown: dict = {}

    for scene in scenes:
        model = _resolve_scene_model_estimate(scene, final_entity_ids, config)
        cost = MODEL_CREDIT_COSTS.get(model, _MUAPI_UNKNOWN_MODEL_COST)
        total_cost += cost
        model_breakdown[model] = model_breakdown.get(model, 0) + 1

    return round(total_cost, 4), model_breakdown


def _append_to_ledger(record: dict, ledger_path: str) -> bool:
    """
    Append a single JSON line to the append-only cost ledger file.

    Returns True on success, False on any I/O error.
    """
    try:
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _write_output(record: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 1 — Budget Authorization
# ---------------------------------------------------------------------------

def run_budget_authorization(
    script_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Pre-run budget gate. Called after Script Engine, before VPD.

    Estimates total spend for this episode from the script_package and
    compares it against FINANCE_BUDGET_CEILING_USD.

    Args:
        script_package: Script Engine output dict.
        episode_id:     Episode identifier (for file naming and logging).
        config:         Loaded Config instance.
        output_dir:     Directory for finance_authorization_{episode_id}.json.
                        Defaults to .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        finance_record dict (phase="authorization") with one of:
            - verdict="AUTHORIZED" — under ceiling (or no ceiling configured)
            - verdict="WARNING"    — above warning threshold but under ceiling
            - verdict="BLOCKED"    — estimate exceeds ceiling; raise FinanceBudgetBlockedError

    The pipeline raises FinanceBudgetBlockedError when verdict=="BLOCKED".
    """
    checks: list = []
    failures: list = []
    warnings: list = []

    # P3-05: cascade factory policy ceiling over env ceiling.
    # Non-blocking — DB/factory unavailability falls back to env value.
    try:
        ceiling = get_effective_budget_ceiling(
            env_ceiling=config.FINANCE_BUDGET_CEILING_USD,
        )
    except Exception:
        ceiling = config.FINANCE_BUDGET_CEILING_USD
    warn_pct = config.FINANCE_WARNING_THRESHOLD_PCT

    # ----------------------------------------------------------------
    # Check 1 — ceiling configured
    # ----------------------------------------------------------------
    if ceiling > 0.0:
        _add_check(checks, failures, warnings,
                   "c_budget_ceiling_configured", "pass",
                   f"Budget ceiling set to ${ceiling:.2f} USD")
    else:
        # No ceiling is a valid operational mode — gate is disabled, not broken
        _add_check(checks, failures, warnings,
                   "c_budget_ceiling_configured", "pass",
                   "No budget ceiling configured (FINANCE_BUDGET_CEILING_USD=0); "
                   "gate disabled — all episodes authorized unconditionally")

    # ----------------------------------------------------------------
    # Check 2 — estimate basis (script_package has sections)
    # ----------------------------------------------------------------
    section_count = _estimate_section_count(script_package)
    narration_chars = _estimate_narration_chars(script_package)

    if section_count > 0:
        _add_check(checks, failures, warnings,
                   "c_estimate_basis", "pass",
                   f"Estimate basis: {section_count} sections, "
                   f"{narration_chars:,} narration characters")
    else:
        _add_check(checks, failures, warnings,
                   "c_estimate_basis", "warn",
                   "script_package has no sections — spend estimate may be zero")

    # ----------------------------------------------------------------
    # Compute cost breakdown
    # ----------------------------------------------------------------
    estimated_narration_usd = (narration_chars / 1000.0) * _ELEVENLABS_COST_PER_1K_CHARS
    estimated_muapi_usd = section_count * _MUAPI_CLIPS_PER_SECTION * _MUAPI_COST_PER_CLIP
    estimated_openrouter_usd = _OPENROUTER_PLANNING_FLAT
    estimated_music_usd = section_count * _MUSIC_COST_PER_SECTION
    total_estimated = (
        estimated_narration_usd
        + estimated_muapi_usd
        + estimated_openrouter_usd
        + estimated_music_usd
    )

    cost_breakdown = {
        "estimated_narration_usd": round(estimated_narration_usd, 4),
        "estimated_muapi_usd": round(estimated_muapi_usd, 4),
        "estimated_openrouter_usd": round(estimated_openrouter_usd, 4),
        "estimated_music_usd": round(estimated_music_usd, 4),
        "total_estimated_usd": round(total_estimated, 4),
        "section_count": section_count,
        "narration_chars": narration_chars,
    }

    # ----------------------------------------------------------------
    # Check 3 — within ceiling
    # ----------------------------------------------------------------
    if ceiling <= 0.0:
        # No ceiling — always pass
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"No ceiling configured; estimated spend ${total_estimated:.4f} USD is unrestricted")
        within_ceiling = True
    elif total_estimated <= ceiling:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"Estimated spend ${total_estimated:.4f} USD ≤ ceiling ${ceiling:.2f} USD")
        within_ceiling = True
    else:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "fail",
                   f"Estimated spend ${total_estimated:.4f} USD exceeds ceiling ${ceiling:.2f} USD "
                   f"(overage: ${total_estimated - ceiling:.4f} USD)")
        within_ceiling = False

    # ----------------------------------------------------------------
    # Check 4 — warning threshold
    # ----------------------------------------------------------------
    if ceiling > 0.0 and within_ceiling and total_estimated > ceiling * warn_pct:
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "warn",
                   f"Estimated spend ${total_estimated:.4f} USD exceeds {warn_pct*100:.0f}% "
                   f"of ceiling ${ceiling:.2f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "pass",
                   f"Spend within warning threshold ({warn_pct*100:.0f}% of ceiling)")

    # ----------------------------------------------------------------
    # Determine verdict
    # ----------------------------------------------------------------
    if not within_ceiling:
        verdict = "BLOCKED"
        authorized = False
    elif warnings:
        verdict = "WARNING"
        authorized = True
    else:
        verdict = "AUTHORIZED"
        authorized = True

    record = {
        "finance_id": f"fa_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "phase": "authorization",
        "recorded_at": _now_iso(),
        "verdict": verdict,
        "authorized": authorized,
        "budget_ceiling_usd": ceiling,
        "estimated_spend_usd": round(total_estimated, 4),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "cost_breakdown": cost_breakdown,
    }

    validate("finance_record", record)

    # Emit OIS signal on BLOCKED or WARNING verdict.
    # Module-level import so patch("hypogeum.ois.emit_signal") works in tests.
    if verdict == "BLOCKED":
        try:
            import hypogeum.ois as _ois
            _ois.emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message=(
                    f"Episode {episode_id!r}: estimated spend "
                    f"${total_estimated:.4f} exceeds ceiling ${ceiling:.2f} USD"
                ),
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                metadata={
                    "episode_id": episode_id,
                    "estimated_usd": round(total_estimated, 4),
                    "ceiling_usd": ceiling,
                },
                episode_id=episode_id,
                config=config,
            )
        except Exception:
            pass
    elif verdict == "WARNING":
        try:
            import hypogeum.ois as _ois
            _ois.emit_signal(
                signal_type="low_budget_warning",
                severity="WARNING",
                message=(
                    f"Episode {episode_id!r}: estimated spend "
                    f"${total_estimated:.4f} is approaching ceiling ${ceiling:.2f} USD"
                ),
                channel_id=None,
                source_system="finance_agent",
                blocking=False,
                metadata={
                    "episode_id": episode_id,
                    "estimated_usd": round(total_estimated, 4),
                    "ceiling_usd": ceiling,
                },
                episode_id=episode_id,
                config=config,
            )
        except Exception:
            pass

    # Write output file
    base = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _write_output(record, Path(base) / f"finance_authorization_{episode_id}.json")

    return record


# ---------------------------------------------------------------------------
# Phase 1b — Post-VPD Budget Authorization (authoritative gate)
# ---------------------------------------------------------------------------

def run_post_vpd_authorization(
    visual_package: dict,
    episode_id: str,
    config: Config,
    script_package: Optional[dict] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Post-VPD authoritative budget gate. Called after Phase 1 (Claude planning)
    and BGA Pass 1, before Phase 2 (MUAPI execution).

    Computes an accurate MUAPI cost estimate from actual scene routing in the
    visual_package plan. This replaces the rough section-count-based estimate
    used by run_budget_authorization() (which runs before VPD has produced
    scene routing data).

    Finance gate ordering:
        run_budget_authorization()    ← rough pre-VPD gate (script_package only)
        run_visual_package_director() ← Phase 1: Claude planning
        BGA Pass 1                    ← brand compliance gate
        run_post_vpd_authorization()  ← authoritative gate (this function)
        MUAPI Phase 2                 ← video generation spend

    Args:
        visual_package: Phase 1 VPD output with scene routing (generation_status = "planned").
        episode_id:     Episode identifier.
        config:         Loaded Config instance.
        script_package: Script Engine output; used to estimate narration + music cost.
                        If None (resume path), narration/music estimated as $0.00.
        output_dir:     Directory for finance_post_vpd_authorization_{episode_id}.json.

    Returns:
        finance_record dict (phase="post_vpd_authorization") with one of:
            - verdict="AUTHORIZED" — total spend within ceiling
            - verdict="WARNING"    — above warning threshold but within ceiling
            - verdict="BLOCKED"    — total spend exceeds ceiling

    The caller must raise FinancePostVPDBlockedError when verdict=="BLOCKED".
    No MUAPI generation should start before this gate returns AUTHORIZED or WARNING.
    """
    checks: list = []
    failures: list = []
    warnings: list = []

    # P3-05: cascade factory policy ceiling (non-blocking fallback to env value)
    try:
        ceiling = get_effective_budget_ceiling(
            env_ceiling=config.FINANCE_BUDGET_CEILING_USD,
        )
    except Exception:
        ceiling = config.FINANCE_BUDGET_CEILING_USD
    warn_pct = config.FINANCE_WARNING_THRESHOLD_PCT

    # ----------------------------------------------------------------
    # Check 1 — ceiling configured
    # ----------------------------------------------------------------
    if ceiling > 0.0:
        _add_check(checks, failures, warnings,
                   "c_budget_ceiling_configured", "pass",
                   f"Budget ceiling set to ${ceiling:.2f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_budget_ceiling_configured", "pass",
                   "No budget ceiling configured; gate disabled — episode authorized unconditionally")

    # ----------------------------------------------------------------
    # Check 2 — VPD plan has scenes
    # ----------------------------------------------------------------
    scenes = visual_package.get("scenes", [])
    scene_count = len(scenes)
    if scene_count > 0:
        _add_check(checks, failures, warnings,
                   "c_vpd_plan_has_scenes", "pass",
                   f"VPD plan has {scene_count} scenes with routing data")
    else:
        _add_check(checks, failures, warnings,
                   "c_vpd_plan_has_scenes", "warn",
                   "VPD plan has no scenes — MUAPI cost estimated as $0.00")

    # ----------------------------------------------------------------
    # Compute authoritative MUAPI cost from scene routing
    # ----------------------------------------------------------------
    estimated_muapi_usd, model_breakdown = _estimate_muapi_from_vpd_plan(
        visual_package, config
    )

    routing_detail = ", ".join(
        f"{model}×{count}" for model, count in sorted(model_breakdown.items())
    )

    _add_check(checks, failures, warnings,
               "c_muapi_routing_estimate", "pass",
               f"MUAPI estimated ${estimated_muapi_usd:.4f} USD from {scene_count} scene(s): "
               f"{routing_detail or 'none'}")

    # ----------------------------------------------------------------
    # Narration + music from script_package (if available)
    # ----------------------------------------------------------------
    if script_package is not None:
        narration_chars = _estimate_narration_chars(script_package)
        section_count = _estimate_section_count(script_package)
        estimated_narration_usd = (narration_chars / 1000.0) * _ELEVENLABS_COST_PER_1K_CHARS
        estimated_music_usd = section_count * _MUSIC_COST_PER_SECTION
    else:
        narration_chars = 0
        section_count = 0
        estimated_narration_usd = 0.0
        estimated_music_usd = 0.0

    estimated_openrouter_usd = _OPENROUTER_PLANNING_FLAT
    total_estimated = (
        estimated_narration_usd
        + estimated_muapi_usd
        + estimated_openrouter_usd
        + estimated_music_usd
    )

    cost_breakdown = {
        "scene_count": scene_count,
        "model_breakdown": model_breakdown,
        "estimated_muapi_usd": round(estimated_muapi_usd, 4),
        "estimated_narration_usd": round(estimated_narration_usd, 4),
        "estimated_music_usd": round(estimated_music_usd, 4),
        "estimated_openrouter_usd": estimated_openrouter_usd,
        "total_estimated_usd": round(total_estimated, 4),
        "narration_chars": narration_chars,
        "section_count": section_count,
    }

    # ----------------------------------------------------------------
    # Check 3 — within ceiling
    # ----------------------------------------------------------------
    if ceiling <= 0.0:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"No ceiling configured; estimated spend ${total_estimated:.4f} USD is unrestricted")
        within_ceiling = True
    elif total_estimated <= ceiling:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"Estimated spend ${total_estimated:.4f} USD ≤ ceiling ${ceiling:.2f} USD")
        within_ceiling = True
    else:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "fail",
                   f"Estimated spend ${total_estimated:.4f} USD exceeds ceiling ${ceiling:.2f} USD "
                   f"(overage: ${total_estimated - ceiling:.4f} USD)")
        within_ceiling = False

    # ----------------------------------------------------------------
    # Check 4 — warning threshold
    # ----------------------------------------------------------------
    if ceiling > 0.0 and within_ceiling and total_estimated > ceiling * warn_pct:
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "warn",
                   f"Estimated spend ${total_estimated:.4f} USD exceeds {warn_pct*100:.0f}% "
                   f"of ceiling ${ceiling:.2f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "pass",
                   f"Spend within warning threshold ({warn_pct*100:.0f}% of ceiling)")

    # ----------------------------------------------------------------
    # Determine verdict
    # ----------------------------------------------------------------
    if not within_ceiling:
        verdict = "BLOCKED"
        authorized = False
    elif warnings:
        verdict = "WARNING"
        authorized = True
    else:
        verdict = "AUTHORIZED"
        authorized = True

    record = {
        "finance_id": f"fv_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "phase": "post_vpd_authorization",
        "recorded_at": _now_iso(),
        "verdict": verdict,
        "authorized": authorized,
        "budget_ceiling_usd": ceiling,
        "estimated_spend_usd": round(total_estimated, 4),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "cost_breakdown": cost_breakdown,
    }

    validate("finance_record", record)

    # Write output file (separate from finance_authorization_{episode_id}.json)
    base = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _write_output(record, Path(base) / f"finance_post_vpd_authorization_{episode_id}.json")

    return record


# ---------------------------------------------------------------------------
# Phase 2 — Cost Ledger
# ---------------------------------------------------------------------------

def run_cost_ledger(
    final_render_package: dict,
    publishing_record: dict,
    archive_record: dict,
    episode_id: str,
    config: Config,
    narration_package: Optional[dict] = None,
    visual_package: Optional[dict] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Post-run cost ledger. Called after Archive Agent. Non-blocking.

    Reads actual cost signals from pipeline artifacts and records a
    finance_record with phase="ledger". If FINANCE_LEDGER_PATH is configured,
    appends the record to the append-only JSONL ledger file.

    Args:
        final_render_package: Assembly Agent output dict.
        publishing_record:    Publishing Agent output dict.
        archive_record:       Archive Agent output dict.
        episode_id:           Episode identifier.
        config:               Loaded Config instance.
        narration_package:    Narration Package dict; provides audio_qa_report.total_characters_billed.
        visual_package:       Visual Package dict; provides asset_manifest for generation counts.
        output_dir:           Directory for finance_record_{episode_id}.json.

    Returns:
        finance_record dict (phase="ledger") with verdict="RECORDED".
        Never raises — missing data produces check status "warn" and cost of $0.00.
    """
    checks: list = []
    failures: list = []
    warnings: list = []

    # ----------------------------------------------------------------
    # Check 1 — narration cost data
    # Source of truth: narration_package.audio_qa_report.total_characters_billed
    # ----------------------------------------------------------------
    narration_chars_billed = _read_narration_chars_billed(narration_package)
    narration_cost_usd = 0.0

    if narration_chars_billed > 0:
        narration_cost_usd = (narration_chars_billed / 1000.0) * _ELEVENLABS_COST_PER_1K_CHARS
        _add_check(checks, failures, warnings,
                   "c_narration_cost_data", "pass",
                   f"Narration: {narration_chars_billed:,} chars billed, "
                   f"${narration_cost_usd:.4f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_narration_cost_data", "warn",
                   "narration_package.audio_qa_report.total_characters_billed absent; "
                   "narration cost recorded as $0.00")

    # ----------------------------------------------------------------
    # Check 2 — MUAPI video generation cost
    # Source of truth: visual_package.asset_manifest.assets[].generation_status
    # Only "generated" assets represent API calls; "reused" assets are free.
    # ----------------------------------------------------------------
    muapi_clips_generated = _count_generated_clips(visual_package)
    muapi_cost_usd = muapi_clips_generated * _MUAPI_COST_PER_CLIP

    if visual_package is not None:
        _add_check(checks, failures, warnings,
                   "c_muapi_cost_data", "pass",
                   f"MUAPI video: {muapi_clips_generated} generated clip(s), "
                   f"${muapi_cost_usd:.4f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_muapi_cost_data", "warn",
                   "visual_package not provided; MUAPI video cost recorded as $0.00")

    # ----------------------------------------------------------------
    # Check 3 — thumbnail generation cost
    # Source of truth: final_render_package.thumbnail_references (count of images)
    # ----------------------------------------------------------------
    thumbnail_cost_usd = _compute_thumbnail_cost(final_render_package)
    thumbnail_count = sum(
        1 for r in final_render_package.get("thumbnail_references", [])
        if r.get("thumbnail_asset_id")
    )

    if thumbnail_count > 0:
        _add_check(checks, failures, warnings,
                   "c_thumbnail_cost_data", "pass",
                   f"Thumbnail: {thumbnail_count} image(s) generated, "
                   f"${thumbnail_cost_usd:.4f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_thumbnail_cost_data", "pass",
                   "Thumbnail: no images generated, $0.0000 USD")

    # ----------------------------------------------------------------
    # Check 4 — music cost
    # channel_library source is free; provider-generated tracks incur cost.
    # ----------------------------------------------------------------
    music_cost_usd = _compute_music_cost(final_render_package)
    music_log = final_render_package.get("music_log", [])
    generated_tracks = sum(
        1 for e in music_log if e.get("source") != _MUSIC_FREE_SOURCE
    )
    library_tracks = len(music_log) - generated_tracks

    if music_log:
        _add_check(checks, failures, warnings,
                   "c_music_cost_data", "pass",
                   f"Music: {library_tracks} library track(s) ($0.00), "
                   f"{generated_tracks} generated track(s) (${music_cost_usd:.4f} USD)")
    else:
        _add_check(checks, failures, warnings,
                   "c_music_cost_data", "pass",
                   "Music: no tracks recorded, $0.0000 USD")

    # ----------------------------------------------------------------
    # Check 5 — actual spend computed
    # ----------------------------------------------------------------
    total_actual_usd = (
        narration_cost_usd
        + muapi_cost_usd
        + thumbnail_cost_usd
        + music_cost_usd
        + _OPENROUTER_PLANNING_FLAT
    )

    any_data = narration_chars_billed > 0 or visual_package is not None
    if any_data:
        _add_check(checks, failures, warnings,
                   "c_actual_spend_computed", "pass",
                   f"Actual spend computed: ${total_actual_usd:.4f} USD")
    else:
        _add_check(checks, failures, warnings,
                   "c_actual_spend_computed", "warn",
                   f"Actual spend is ${total_actual_usd:.4f} USD — cost signals were incomplete")

    # ----------------------------------------------------------------
    # Check 6 — ledger appended
    # ----------------------------------------------------------------
    ledger_path = config.FINANCE_LEDGER_PATH
    if ledger_path:
        ledger_line = {
            "episode_id": episode_id,
            "recorded_at": _now_iso(),
            "total_actual_usd": round(total_actual_usd, 4),
            "narration_chars_billed": narration_chars_billed,
            "muapi_clips_generated": muapi_clips_generated,
            "thumbnail_cost_usd": round(thumbnail_cost_usd, 4),
            "music_cost_usd": round(music_cost_usd, 4),
        }
        appended = _append_to_ledger(ledger_line, ledger_path)
        if appended:
            _add_check(checks, failures, warnings,
                       "c_ledger_appended", "pass",
                       f"Cost record appended to ledger: {ledger_path}")
        else:
            _add_check(checks, failures, warnings,
                       "c_ledger_appended", "warn",
                       f"Failed to append to ledger: {ledger_path}")
    else:
        _add_check(checks, failures, warnings,
                   "c_ledger_appended", "warn",
                   "FINANCE_LEDGER_PATH not configured; cost record not persisted to ledger")

    # ----------------------------------------------------------------
    # Verdict — ledger phase always RECORDED (non-blocking)
    # ----------------------------------------------------------------
    verdict = "RECORDED"
    authorized = True

    cost_breakdown = {
        "narration_chars_billed": narration_chars_billed,
        "narration_cost_usd": round(narration_cost_usd, 4),
        "muapi_clips_generated": muapi_clips_generated,
        "muapi_cost_usd": round(muapi_cost_usd, 4),
        "thumbnail_cost_usd": round(thumbnail_cost_usd, 4),
        "music_cost_usd": round(music_cost_usd, 4),
        "openrouter_flat_usd": _OPENROUTER_PLANNING_FLAT,
        "total_actual_usd": round(total_actual_usd, 4),
    }

    # P3-05: use cascaded ceiling so ledger records the same effective ceiling
    # that the authorization gate used. Non-blocking fallback to env value.
    try:
        ceiling = get_effective_budget_ceiling(
            env_ceiling=config.FINANCE_BUDGET_CEILING_USD,
        )
    except Exception:
        ceiling = config.FINANCE_BUDGET_CEILING_USD

    record = {
        "finance_id": f"fl_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "phase": "ledger",
        "recorded_at": _now_iso(),
        "verdict": verdict,
        "authorized": authorized,
        "budget_ceiling_usd": ceiling,
        "estimated_spend_usd": round(total_actual_usd, 4),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "cost_breakdown": cost_breakdown,
    }

    validate("finance_record", record)

    # Write output file
    base = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _write_output(record, Path(base) / f"finance_record_{episode_id}.json")

    return record
