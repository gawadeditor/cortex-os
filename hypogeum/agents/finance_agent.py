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

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.failure_policy import FailurePolicy
from hypogeum.policy_cascade import get_effective_budget_ceiling, get_per_episode_ceilings
from hypogeum.production_plan import (
    ROLE_CONTINGENCY,
    ROLE_REQUIRED,
    ProductionPlan,
    load_production_plan,
)
from hypogeum.schemas import validate

# ---------------------------------------------------------------------------
# Cost constants (from benchmarks — memory: Seedance benchmark 2026-06-06)
# ---------------------------------------------------------------------------

_ELEVENLABS_COST_PER_1K_CHARS: float = 0.30   # USD per 1,000 characters billed
_MUAPI_COST_PER_CLIP: float = 0.15            # USD per clip (Runway rate, conservative) —
                                               # used only by run_cost_ledger's post-hoc
                                               # fallback; the real per-provider spend of
                                               # record is the `costs` DB table, so this
                                               # rough constant was left as-is in the
                                               # 2026-09-19 fix (see _resolve_clip_price
                                               # for the channel-real-price version used by
                                               # the pre-spend run_budget_authorization gate).
_MUAPI_DEFAULT_TIER_CLIP_PRICE_FALLBACK: float = 0.39  # USD/clip when a channel has no
                                               # adopted model yet — the global default tier
                                               # (veo3.1-lite-text-to-video)'s real catalogue
                                               # price, not an unrelated flat guess.
_OPENROUTER_PLANNING_FLAT: float = 0.05       # flat estimate for all LLM planning calls
_MUSIC_TRACKS_ASSUMED: int = 1                # one continuous background track per main
                                               # episode is the observed pattern (see
                                               # _estimate_beat_count's sibling rationale)

# Image model costs (USD per image generation call)
_MUAPI_IMAGE_COSTS: dict = {
    "flux-dev-image":     0.01,
    "flux-schnell-image": 0.005,
}
_MUAPI_IMAGE_DEFAULT_COST: float = 0.01   # fallback for unknown image models

# Music source that incurs no provider cost
_MUSIC_FREE_SOURCE: str = "channel_library"
_ELEVENLABS_MUSIC_COST_PER_TRACK: float = 0.20   # USD per generated music track

# Bumped whenever the cost constants above or the estimation formula in
# run_budget_authorization() change in a way that would alter its output for
# the same script_package/ceiling -- part of the checkpoint binding key below,
# so a pricing change explicitly invalidates any existing checkpointed
# authorization rather than being silently masked by checkpoint reuse.
_FINANCE_AUTHORIZATION_BINDING_VERSION: str = "1.1"  # 2026-09-19: beat/real-price muapi+music fix


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_script_package_checksum(script_package: dict) -> str:
    """
    Deterministic sha256 of script_package's canonical JSON serialization --
    the checkpoint-binding key's primary input. Same sort_keys+compact-separator
    convention used for checksums elsewhere in this codebase.
    """
    canonical = json.dumps(
        script_package, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _estimate_beat_count(script_package: dict) -> int:
    """Total main-episode narration beats across every section — one beat
    becomes one scene becomes one paid video clip (see hypogeum.muapi and
    hypogeum.agents.audio_agent._collect_main_beats for the same convention
    elsewhere in the pipeline). Section COUNT alone says nothing about how
    many beats a section holds — a 6-section long-form episode can have
    31+ beats total where a 6-"section" short has 6-8, so section_count was
    silently treating both as the same clip budget (2026-09-19 fix, found
    while testing the twinkle long-form workflow: a real 31-beat episode
    priced out at a 6-clip short's cost)."""
    return sum(
        len(section.get("narration_beats", []))
        for section in script_package.get("sections", [])
    )


def _resolve_clip_price(channel_id: Optional[str], config: Config, db_path: Optional[Path]) -> float:
    """The USD price of one video clip for this channel: its own adopted
    model's captured price when it has one, else the global default tier's
    real catalogue price — never a flat cross-channel placeholder. A channel
    on a $0.65/clip model must not be estimated at some other model's rate."""
    if channel_id:
        try:
            from hypogeum.channel_config import resolve_channel_video_model
            _model, price = resolve_channel_video_model(channel_id, db_path=db_path)
            if price:
                return float(price)
        except Exception:  # noqa: BLE001
            pass
    try:
        from hypogeum.muapi import MODEL_CREDIT_COSTS, _tier_to_model
        model = _tier_to_model(config.MUAPI_DEFAULT_TIER, None, config)
        return float(MODEL_CREDIT_COSTS.get(model, _MUAPI_DEFAULT_TIER_CLIP_PRICE_FALLBACK))
    except Exception:  # noqa: BLE001
        return _MUAPI_DEFAULT_TIER_CLIP_PRICE_FALLBACK


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

    TERMINAL: a BLOCKED verdict means the compiled plan's bounded cost
    exceeds the effective per-episode ceiling. Both inputs are stable —
    the plan is content-hash-locked and the ceiling is channel/factory
    config — so re-running the pipeline reproduces the block identically
    while re-burning every pre-VPD paid stage (research, script,
    narration, VPD planning). The operator must lower the plan cost or
    raise the ceiling; no retry can clear it. (Observed 2026-09-06:
    ep_734076a3 auto-retried a $27.72-vs-$25 block.)
    """
    failure_policy = FailurePolicy.TERMINAL

    def __init__(self, episode_id: str, record: dict):
        self.episode_id = episode_id
        self.record = record
        super().__init__(
            f"Post-VPD finance gate BLOCKED for {episode_id}: "
            f"estimated ${record.get('estimated_spend_usd', '?')} USD "
            f"exceeds ${record.get('budget_ceiling_usd', '?')} USD ceiling"
        )


class FinancePostVPDPlanMismatchError(Exception):
    """
    Raised by run_post_vpd_authorization() when the production_plan passed
    in is not byte-for-byte the same compiled artifact as the episode's
    verified persisted Production Plan (or no persisted plan exists).
    Phase 2 plan-bound authorization: authorization may only be calculated
    from the exact verified persisted plan -- an in-memory plan that
    diverges from what was persisted (a replaced or recompiled plan) is
    never authorized. Halts the pipeline before any spend.
    """
    def __init__(self, episode_id: str, passed_hash: str, persisted_hash: str):
        self.episode_id = episode_id
        self.passed_content_hash = passed_hash
        self.persisted_content_hash = persisted_hash
        super().__init__(
            f"Post-VPD finance gate plan mismatch for {episode_id}: passed "
            f"production_plan content_hash={passed_hash!r} != verified persisted "
            f"plan content_hash={persisted_hash!r} -- authorization is calculated "
            f"only from the exact verified persisted Production Plan"
        )


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
    db_path: Optional[Path] = None,
    channel_id: Optional[str] = None,
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
        db_path:        Override DB path for the factory-policy ceiling
                         cascade (see policy_cascade.get_effective_budget_ceiling).
                         None (default) resolves to the production DB, which
                         is correct for real pipeline runs; tests must always
                         pass an isolated path.
        channel_id:     Channel identifier used to price the video estimate at
                         this channel's own adopted model rate (see
                         _resolve_clip_price). None = global default tier price.

    Returns:
        finance_record dict (phase="authorization") with one of:
            - verdict="AUTHORIZED" — under ceiling (or no ceiling configured)
            - verdict="WARNING"    — above warning threshold but under ceiling
            - verdict="BLOCKED"    — estimate exceeds ceiling; raise FinanceBudgetBlockedError

    Checkpoint reuse (2026-08-17): unlike Executive Producer/Research
    Director/etc., this stage has no upstream _load_package() gate in
    pipeline.py, so every resumed run re-executed it unconditionally even
    when script_package and the effective ceiling were byte-for-byte
    identical -- producing a new finance_id/recorded_at (and, once, a
    genuinely different narration_chars count from a run whose in-memory
    script_package was never itself checksum-bound at the time, so its
    provenance could not be verified after the fact). An existing
    finance_authorization_{episode_id}.json is now reused verbatim (returned
    unread-modified, so the file bytes never change) only when its
    bound_script_package_checksum, bound_budget_ceiling_usd, and
    bound_finance_agent_version all match the current inputs exactly. Any
    mismatch explicitly invalidates the prior authorization -- the new
    record records supersedes_finance_id/invalidation_reason rather than
    silently overwriting it with no trace.

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
            db_path=db_path,
        )
    except Exception:
        ceiling = config.FINANCE_BUDGET_CEILING_USD
    warn_pct = config.FINANCE_WARNING_THRESHOLD_PCT

    # ----------------------------------------------------------------
    # Checkpoint reuse — bound to explicit checksums/config, not implicit
    # ----------------------------------------------------------------
    _base = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _existing_path = Path(_base) / f"finance_authorization_{episode_id}.json"
    _script_package_checksum = _compute_script_package_checksum(script_package)
    _existing_record: Optional[dict] = None
    if _existing_path.exists():
        try:
            _existing_record = json.loads(_existing_path.read_text(encoding="utf-8"))
        except Exception:
            _existing_record = None
    if (
        _existing_record is not None
        and _existing_record.get("bound_script_package_checksum") == _script_package_checksum
        and _existing_record.get("bound_budget_ceiling_usd") == ceiling
        and _existing_record.get("bound_finance_agent_version")
            == _FINANCE_AUTHORIZATION_BINDING_VERSION
    ):
        return _existing_record

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
    beat_count = _estimate_beat_count(script_package)
    narration_chars = _estimate_narration_chars(script_package)
    clip_price = _resolve_clip_price(channel_id, config, db_path)

    if section_count > 0:
        _add_check(checks, failures, warnings,
                   "c_estimate_basis", "pass",
                   f"Estimate basis: {section_count} sections, {beat_count} beats "
                   f"(= planned clips), {narration_chars:,} narration characters, "
                   f"${clip_price:.2f}/clip")
    else:
        _add_check(checks, failures, warnings,
                   "c_estimate_basis", "warn",
                   "script_package has no sections — spend estimate may be zero")

    # ----------------------------------------------------------------
    # Compute cost breakdown
    # ----------------------------------------------------------------
    # 2026-09-19: estimated_muapi_usd used to be section_count * a fixed
    # "3 clips/section" guess at a flat $0.15 "Runway rate" — a short's clip
    # budget applied unchanged to every episode regardless of actual length,
    # and ignoring the channel's real adopted model price. One beat is one
    # planned video clip (see _estimate_beat_count's docstring); price it at
    # what this channel's clips actually cost.
    estimated_narration_usd = (narration_chars / 1000.0) * _ELEVENLABS_COST_PER_1K_CHARS
    estimated_muapi_usd = beat_count * clip_price
    estimated_openrouter_usd = _OPENROUTER_PLANNING_FLAT
    estimated_music_usd = _MUSIC_TRACKS_ASSUMED * _ELEVENLABS_MUSIC_COST_PER_TRACK
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
        "beat_count": beat_count,
        "clip_price_usd": round(clip_price, 4),
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
        # Checkpoint-binding key -- see run_budget_authorization()'s own
        # docstring. Reused verbatim on the next call iff all three match.
        "bound_script_package_checksum": _script_package_checksum,
        "bound_budget_ceiling_usd": ceiling,
        "bound_finance_agent_version": _FINANCE_AUTHORIZATION_BINDING_VERSION,
    }

    # Explicit invalidation evidence: this call is only reached when either
    # no prior authorization existed, or one existed but its binding key
    # didn't match (the checkpoint-reuse return above already handled the
    # matching case) -- record exactly what changed rather than silently
    # overwriting a prior authorization with no trace.
    if _existing_record is not None:
        record["supersedes_finance_id"] = _existing_record.get("finance_id")
        _changed = []
        if _existing_record.get("bound_script_package_checksum") != _script_package_checksum:
            _changed.append("script_package")
        if _existing_record.get("bound_budget_ceiling_usd") != ceiling:
            _changed.append("budget_ceiling")
        if _existing_record.get("bound_finance_agent_version") != _FINANCE_AUTHORIZATION_BINDING_VERSION:
            _changed.append("finance_agent_version")
        record["invalidation_reason"] = (
            f"prior authorization {_existing_record.get('finance_id')!r} invalidated: "
            f"changed inputs: {', '.join(_changed) if _changed else 'unknown (binding fields missing on prior record)'}"
        )

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
    _write_output(record, _existing_path)

    return record


# ---------------------------------------------------------------------------
# Phase 1b — Post-VPD Budget Authorization (authoritative gate)
# ---------------------------------------------------------------------------

def run_post_vpd_authorization(
    visual_package: dict,
    episode_id: str,
    config: Config,
    production_plan: "ProductionPlan",
    output_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Post-VPD authoritative budget gate. Called after Phase 1 (Claude planning),
    Production Plan compilation + coverage validation, and BGA Pass 1, before
    Phase 2 (MUAPI execution).

    production_plan is REQUIRED (2026-08-24 factory-level correction — the
    fix for the ep_af078f34 root cause's Finance half, and the removal of
    the None-tolerant legacy paid path per the follow-up architecture
    correction). There is no fallback estimate any more: this gate
    authorizes exclusively against the compiled hypogeum.production_plan.
    ProductionPlan's own cost-complete totals —
    normal_expected_cost_usd (every billable category, required assets at
    one attempt each) and maximum_bounded_cost_usd (every billable
    category, required + declared contingency assets at their full
    max_chargeable_attempts bound). Both totals already include video
    generation, source enhancement, thumbnails, narration, music, and
    planning-call estimates — see hypogeum.production_plan's own module
    docstring for the authoritative price source of each category. This
    function does NOT re-derive or append narration/music/OpenRouter costs
    from script_package: doing so would double-count categories the plan
    already prices, and would let those categories drift onto invented
    constants instead of the plan's own authoritative sources.

    A caller with no compiled plan (a legacy checkpoint, an episode from
    before this correction, or a resume path that has not reconciled its
    plan) cannot call this function at all — TypeError at the call site
    for a missing argument. This is deliberate: there is no institutional
    way to authorize spend without a plan any more. See
    hypogeum.production_plan.load_production_plan() for how a resume path
    should recover an existing plan, and
    hypogeum.agents.visual_package_director.ProductionPlanMissingError for
    the fail-closed error a resume path raises when no plan can be found.

    Finance gate ordering:
        run_budget_authorization()    ← rough pre-VPD gate (script_package only)
        run_visual_package_director() ← Phase 1: Claude planning
        compile_production_plan() +
          validate_plan_coverage()    ← compiled, coverage-validated plan
        BGA Pass 1                    ← brand compliance gate
        run_post_vpd_authorization()  ← authoritative gate (this function)
        muapi._submit_scene_via_gate()← plan-conformance check + grant/envelope gate
        MUAPI Phase 2                 ← video generation spend

    Args:
        visual_package:  Phase 1 VPD output with scene routing (generation_status = "planned").
        episode_id:      Episode identifier.
        config:          Loaded Config instance.
        production_plan: hypogeum.production_plan.ProductionPlan — the
                         immutable, cost-complete compiled plan this gate
                         authorizes against. Required.
        output_dir:      Directory for finance_post_vpd_authorization_{episode_id}.json.
        db_path:         Override DB path for the factory-policy ceiling
                         cascade (see policy_cascade.get_effective_budget_ceiling).
                         None (default) resolves to the production DB, which
                         is correct for real pipeline runs; tests must always
                         pass an isolated path.

    Returns:
        finance_record dict (phase="post_vpd_authorization") with one of:
            - verdict="AUTHORIZED" — maximum bounded spend within ceiling
            - verdict="WARNING"    — above warning threshold but within ceiling
            - verdict="BLOCKED"    — maximum bounded spend exceeds ceiling

    The caller must raise FinancePostVPDBlockedError when verdict=="BLOCKED".
    No MUAPI generation should start before this gate returns AUTHORIZED or WARNING.
    """
    checks: list = []
    failures: list = []
    warnings: list = []

    # ----------------------------------------------------------------
    # Phase 2 plan-bound authorization -- verify the passed plan IS the
    # exact verified persisted Production Plan before authorizing
    # anything against it. load_production_plan() re-hashes the payload
    # and fails closed (ProductionPlanIntegrityError) on any tampering;
    # a missing file, or a persisted plan whose verified content_hash
    # differs from the caller's in-memory object (a replaced or
    # recompiled plan), raises FinancePostVPDPlanMismatchError. The
    # verified persisted hash is recorded on the finance_record as the
    # durable bound hash the MUAPI video gate later three-way-checks
    # against the grant and the loaded plan.
    # ----------------------------------------------------------------
    _base = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _persisted_plan = load_production_plan(episode_id, Path(_base))
    if _persisted_plan is None:
        raise FinancePostVPDPlanMismatchError(
            episode_id=episode_id,
            passed_hash=production_plan.content_hash,
            persisted_hash="<no persisted production plan>",
        )
    if _persisted_plan.content_hash != production_plan.content_hash:
        raise FinancePostVPDPlanMismatchError(
            episode_id=episode_id,
            passed_hash=production_plan.content_hash,
            persisted_hash=_persisted_plan.content_hash,
        )

    # Two-tier per-episode ceiling (2026-09-06). The compiled plan reports a
    # normal_expected_cost_usd and a much larger maximum_bounded_cost_usd
    # (every scene exhausting its paid-retry budget). Gate them SEPARATELY:
    #   * normal_expected  <=  operator_ceiling  (the channel's own
    #     cost_ceiling_usd_per_episode, threaded in as FINANCE_BUDGET_CEILING_USD
    #     after hypogeum.pipeline's per-run override) — "a normally-priced episode"
    #   * maximum_bounded   <=  factory_ceiling  (monthly_ceiling_usd /
    #     episodes_per_month) — the higher hard bound that still contains a
    #     genuine runaway, alongside the channel WEEKLY reservation and
    #     hypogeum.muapi_spend_breaker.
    # The old min()-collapse gated the operator's dial on the worst case,
    # making a sane per-episode ceiling impossible (an operator had to
    # budget ~2x a normal episode to allow one) — which blocked EVERY
    # hypogeum episode once per-scene paid retry was enabled (ep_734076a3).
    try:
        operator_ceiling, factory_ceiling = get_per_episode_ceilings(
            env_ceiling=config.FINANCE_BUDGET_CEILING_USD, db_path=db_path,
        )
    except Exception:
        operator_ceiling, factory_ceiling = config.FINANCE_BUDGET_CEILING_USD, 0.0
    # the ceiling the MAXIMUM bounded total is judged against — factory when
    # configured, else fall back to the operator ceiling (unchanged behavior
    # for a factory with no monthly policy).
    bounded_ceiling = factory_ceiling if factory_ceiling > 0.0 else operator_ceiling
    ceiling = bounded_ceiling  # kept for the record's budget_ceiling_usd / remaining_budget_usd
    warn_pct = config.FINANCE_WARNING_THRESHOLD_PCT

    # ----------------------------------------------------------------
    # Check 1 — ceiling configured
    # ----------------------------------------------------------------
    if operator_ceiling > 0.0 or bounded_ceiling > 0.0:
        _add_check(checks, failures, warnings,
                   "c_budget_ceiling_configured", "pass",
                   f"Per-episode ceilings: expected ≤ ${operator_ceiling:.2f} USD "
                   f"(operator), bounded ≤ ${bounded_ceiling:.2f} USD (factory)")
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

    # ------------------------------------------------------------
    # Plan-driven costing — the plan's own totals ARE the total. No
    # category is re-derived or appended here: narration, music, and
    # planning-call costs are already inside normal_expected_cost_usd /
    # maximum_bounded_cost_usd (see hypogeum.production_plan).
    # ------------------------------------------------------------
    normal_expected_usd = production_plan.normal_expected_cost_usd
    maximum_bounded_usd = production_plan.maximum_bounded_cost_usd
    contingency_usd = round(maximum_bounded_usd - normal_expected_usd, 4)
    required_count = sum(1 for a in production_plan.assets if a.role == ROLE_REQUIRED)
    contingency_count = sum(1 for a in production_plan.assets if a.role == ROLE_CONTINGENCY)
    bounded_technical_retry_allowance = sum(
        a.max_chargeable_attempts for a in production_plan.assets
    )

    _add_check(checks, failures, warnings,
               "c_production_plan_costed", "pass",
               f"Production Plan {production_plan.content_hash[:12]}: "
               f"{required_count} required + {contingency_count} contingency asset(s), "
               f"thumbnails ${production_plan.thumbnail_cost_normal_usd:.4f}-"
               f"${production_plan.thumbnail_cost_maximum_usd:.4f} USD -- "
               f"normal ${normal_expected_usd:.4f} USD, "
               f"maximum bounded ${maximum_bounded_usd:.4f} USD")

    total_estimated = maximum_bounded_usd
    model_breakdown: dict = {}
    for a in production_plan.assets:
        model_breakdown[a.model] = model_breakdown.get(a.model, 0) + 1

    cost_breakdown = {
        "scene_count": scene_count,
        "model_breakdown": model_breakdown,
        "estimated_muapi_usd": round(maximum_bounded_usd, 4),
        "estimated_narration_usd": round(production_plan.narration_cost_usd, 4),
        "estimated_music_usd": round(production_plan.music_cost_usd, 4),
        "estimated_openrouter_usd": round(production_plan.planning_call_estimate_usd, 4),
        "total_estimated_usd": round(total_estimated, 4),
        "narration_chars": production_plan.narration_char_count,
        "production_plan_content_hash": production_plan.content_hash,
        "required_asset_count": required_count,
        "contingency_asset_count": contingency_count,
        "thumbnail_cost_normal_usd": round(production_plan.thumbnail_cost_normal_usd, 4),
        "thumbnail_cost_maximum_usd": round(production_plan.thumbnail_cost_maximum_usd, 4),
        "normal_expected_cost_usd": round(normal_expected_usd, 4),
        "declared_contingency_cost_usd": contingency_usd,
        "bounded_technical_retry_allowance": bounded_technical_retry_allowance,
        "maximum_bounded_cost_usd": round(total_estimated, 4),
        "operator_ceiling_usd": round(operator_ceiling, 4),
        "factory_ceiling_usd": round(bounded_ceiling, 4),
        "remaining_budget_usd": round(ceiling - total_estimated, 4) if ceiling > 0 else None,
    }

    # ----------------------------------------------------------------
    # Check 3 — within ceiling: expected vs operator, bounded vs factory
    # ----------------------------------------------------------------
    expected_ok = operator_ceiling <= 0.0 or normal_expected_usd <= operator_ceiling
    bounded_ok = bounded_ceiling <= 0.0 or maximum_bounded_usd <= bounded_ceiling
    within_ceiling = expected_ok and bounded_ok
    if operator_ceiling <= 0.0 and bounded_ceiling <= 0.0:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"No ceiling configured; estimated spend ${total_estimated:.4f} USD is unrestricted")
    elif within_ceiling:
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "pass",
                   f"Expected ${normal_expected_usd:.4f} ≤ operator ${operator_ceiling:.2f}; "
                   f"bounded ${maximum_bounded_usd:.4f} ≤ factory ${bounded_ceiling:.2f} USD")
    else:
        _reasons = []
        if not expected_ok:
            _reasons.append(
                f"expected ${normal_expected_usd:.4f} exceeds operator ceiling "
                f"${operator_ceiling:.2f} (over ${normal_expected_usd - operator_ceiling:.4f})"
            )
        if not bounded_ok:
            _reasons.append(
                f"maximum bounded ${maximum_bounded_usd:.4f} exceeds factory ceiling "
                f"${bounded_ceiling:.2f} (over ${maximum_bounded_usd - bounded_ceiling:.4f})"
            )
        _add_check(checks, failures, warnings,
                   "c_within_ceiling", "fail", "; ".join(_reasons))

    # ----------------------------------------------------------------
    # Check 4 — warning threshold: the retry contingency alone pushes the
    # worst case past the operator's normal-episode budget (not a block —
    # the factory ceiling above is the hard bound — but worth surfacing).
    # ----------------------------------------------------------------
    if (
        within_ceiling and operator_ceiling > 0.0
        and maximum_bounded_usd > operator_ceiling
    ):
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "warn",
                   f"Worst-case bounded spend ${maximum_bounded_usd:.4f} USD exceeds the "
                   f"operator per-episode ceiling ${operator_ceiling:.2f} USD "
                   f"(expected ${normal_expected_usd:.4f} is within it)")
    elif within_ceiling and operator_ceiling > 0.0 and normal_expected_usd > operator_ceiling * warn_pct:
        _add_check(checks, failures, warnings,
                   "c_warning_threshold", "warn",
                   f"Expected spend ${normal_expected_usd:.4f} USD exceeds {warn_pct*100:.0f}% "
                   f"of the operator ceiling ${operator_ceiling:.2f} USD")
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
        # Phase 2 plan-bound authorization: the verified persisted
        # Production Plan this authorization was calculated from. The
        # MUAPI video gate three-way-checks this hash against the grant's
        # registered production_plan_content_hash and the freshly loaded,
        # integrity-verified plan before any paid submission.
        "bound_production_plan_content_hash": production_plan.content_hash,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "cost_breakdown": cost_breakdown,
    }

    validate("finance_record", record)

    # Write output file (separate from finance_authorization_{episode_id}.json)
    _write_output(record, Path(_base) / f"finance_post_vpd_authorization_{episode_id}.json")

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
    run_id: Optional[str] = None,
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
        "run_id": run_id,
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
